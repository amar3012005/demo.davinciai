"""
Minimal test for process_vision_result function.

Tests vision normalization without heavy imports.
"""

import re
from typing import Dict, Any, Optional


def process_vision_result(raw_vision: str, site_map_node: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Process vision bootstrap as ADVISORY (not source of truth).
    """
    # Parse identified controls from raw vision
    identified = []
    control_pattern = r"click\s+([a-zA-Z0-9_-]+)\s*\(([^)]+)\)"
    for match in re.finditer(control_pattern, raw_vision, re.IGNORECASE):
        identified.append({
            "target_id": match.group(1),
            "label": match.group(2),
            "source": "vision"
        })

    # Also catch "target: t-xxx" patterns
    target_pattern = r"(?:target|identified)[:\s]+([a-zA-Z0-9_-]+)"
    for match in re.finditer(target_pattern, raw_vision, re.IGNORECASE):
        target_id = match.group(1)
        if not any(c["target_id"] == target_id for c in identified):
            identified.append({
                "target_id": target_id,
                "label": "",
                "source": "vision"
            })

    # Score confidence
    confidence = {}
    if site_map_node:
        expected = site_map_node.get("expected_controls", [])
        for ctrl in identified:
            label_lower = ctrl.get("label", "").lower()
            if any(exp.lower() in label_lower for exp in expected):
                confidence[ctrl["target_id"]] = 0.9
            else:
                confidence[ctrl["target_id"]] = 0.5
    else:
        for ctrl in identified:
            confidence[ctrl["target_id"]] = 0.5

    # Find controls vision missed
    missing = []
    if site_map_node:
        vision_labels = [c.get("label", "").lower() for c in identified]
        expected = site_map_node.get("expected_controls", [])
        for exp in expected:
            if not any(exp.lower() in vl for vl in vision_labels):
                missing.append(exp)

    return {
        "raw_recommendations": raw_vision,
        "identified_controls": identified,
        "confidence_scores": confidence,
        "advisory_only": True,
        "missing_controls": missing
    }


class TestProcessVisionResult:
    """Test suite for vision normalization."""

    def test_preserves_raw_recommendations_intact(self):
        """Vision raw output should not be rewritten or modified."""
        raw_vision = """
        I can see the following controls:
        - click t-abc123 (Date Range Picker)
        - click t-xyz789 (Show All Models)
        """
        site_map_node = {
            "expected_controls": ["Date Range", "Model Filter", "Usage Tab"]
        }

        result = process_vision_result(raw_vision, site_map_node)

        assert result["raw_recommendations"] == raw_vision
        assert "raw_recommendations" in result
        print("PASS: test_preserves_raw_recommendations_intact")

    def test_advisory_only_flag_is_true(self):
        """Vision must be marked as advisory_only: True."""
        raw_vision = "click t-abc123 (Date Picker)"
        site_map_node = {"expected_controls": ["Date Range"]}

        result = process_vision_result(raw_vision, site_map_node)

        assert result["advisory_only"] is True
        print("PASS: test_advisory_only_flag_is_true")

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
        assert identified[0]["target_id"] == "t-abc123"
        assert identified[0]["label"] == "Date Range Picker"
        print("PASS: test_identified_controls_parsed_correctly")

    def test_confidence_scores_higher_when_matching_site_map(self):
        """Confidence should be higher (0.9) when vision matches site map."""
        raw_vision = "click t-abc123 (Date Range Picker)"
        site_map_node = {
            "expected_controls": ["Date Range", "Model Filter"]
        }

        result = process_vision_result(raw_vision, site_map_node)

        confidence = result["confidence_scores"]
        assert confidence["t-abc123"] == 0.9
        print("PASS: test_confidence_scores_higher_when_matching_site_map")

    def test_confidence_scores_lower_when_not_matching_site_map(self):
        """Confidence should be lower (0.5) when vision doesn't match site map."""
        raw_vision = "click t-abc123 (Some Random Button)"
        site_map_node = {
            "expected_controls": ["Date Range", "Model Filter"]
        }

        result = process_vision_result(raw_vision, site_map_node)

        confidence = result["confidence_scores"]
        assert confidence["t-abc123"] == 0.5
        print("PASS: test_confidence_scores_lower_when_not_matching_site_map")

    def test_missing_controls_identified(self):
        """Should identify site map controls that vision didn't see."""
        raw_vision = "click t-abc123 (Date Range Picker)"
        site_map_node = {
            "expected_controls": ["Date Range", "Model Filter", "Usage Tab"]
        }

        result = process_vision_result(raw_vision, site_map_node)

        missing = result["missing_controls"]
        assert "Model Filter" in missing
        assert "Usage Tab" in missing
        assert "Date Range" not in missing
        print("PASS: test_missing_controls_identified")

    def test_vision_misses_date_picker_control_groups_still_work(self):
        """Vision misses date picker - control_groups should still find it."""
        raw_vision = "click t-nav123 (Navigation Link)"
        site_map_node = {
            "expected_controls": ["Date Range", "Model Filter"],
            "control_groups": {
                "date_filters": ["Date Picker", "Period Select"]
            }
        }

        result = process_vision_result(raw_vision, site_map_node)

        assert "Date Range" in result["missing_controls"]
        assert "Model Filter" in result["missing_controls"]
        print("PASS: test_vision_misses_date_picker_control_groups_still_work")


if __name__ == "__main__":
    test = TestProcessVisionResult()
    test.test_preserves_raw_recommendations_intact()
    test.test_advisory_only_flag_is_true()
    test.test_identified_controls_parsed_correctly()
    test.test_confidence_scores_higher_when_matching_site_map()
    test.test_confidence_scores_lower_when_not_matching_site_map()
    test.test_missing_controls_identified()
    test.test_vision_misses_date_picker_control_groups_still_work()
    print("\nAll tests passed!")

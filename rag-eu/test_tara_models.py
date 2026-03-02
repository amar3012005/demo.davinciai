#!/usr/bin/env python3
"""
test_tara_models.py

Unit tests and integration examples for tara_models.py
Tests all dataclasses, serialization, and helper methods.

Run: python test_tara_models.py
"""

import json
import time
from tara_models import (
    ActionIntent,
    TacticalSchema,
    StrategyHint,
    VisualHint,
    HiveResponse,
    GraphNode,
    ConstraintStatus,
    Constraint,
    MissionState,
    ScoredCandidate,
    DetectiveReport,
    DomDelta,
    serialize_for_redis,
    deserialize_from_json
)


def test_action_intent_enum():
    """Test ActionIntent enum values."""
    print("\n=== Test: ActionIntent Enum ===")
    assert ActionIntent.PURCHASE.value == "purchase"
    assert ActionIntent.NAVIGATION.value == "navigation"
    assert ActionIntent.EXTRACTION.value == "extraction"
    assert ActionIntent.INTERACTION.value == "interaction"
    assert ActionIntent.SEARCH.value == "search"
    print("✅ ActionIntent enum values correct")


def test_tactical_schema():
    """Test TacticalSchema creation and methods."""
    print("\n=== Test: TacticalSchema ===")
    
    schema = TacticalSchema(
        action=ActionIntent.PURCHASE,
        target_entity="T-shirt",
        domain="zalando.com",
        constraints={"color": "white", "size": None, "quantity": None},
        raw_utterance="Buy a white shirt"
    )
    
    # Test missing_constraints()
    missing = schema.missing_constraints()
    assert "size" in missing, f"Expected 'size' in missing, got {missing}"
    assert "quantity" in missing, f"Expected 'quantity' in missing, got {missing}"
    assert "color" not in missing, f"Expected 'color' NOT in missing, got {missing}"
    print(f"✅ missing_constraints() works: {missing}")
    
    # Test has_all_constraints()
    assert not schema.has_all_constraints(), "Expected has_all_constraints() to be False"
    print("✅ has_all_constraints() works")
    
    # Test to_query_string()
    query = schema.to_query_string()
    assert "action:purchase" in query
    assert "domain:zalando.com" in query
    assert "color:white" in query
    assert "size" not in query  # Missing constraints excluded
    print(f"✅ to_query_string() works: {query}")
    
    # Test to_dict/from_dict
    data = schema.to_dict()
    assert data["action"] == "purchase"
    assert data["target_entity"] == "T-shirt"
    
    schema2 = TacticalSchema.from_dict(data)
    assert schema2.action == schema.action
    assert schema2.target_entity == schema.target_entity
    assert schema2.constraints == schema.constraints
    print("✅ to_dict/from_dict serialization works")
    
    return schema


def test_strategy_hint():
    """Test StrategyHint dataclass."""
    print("\n=== Test: StrategyHint ===")
    
    strategy = StrategyHint(
        sequence=["Search", "Select Item", "Choose Size", "Add to Bag"],
        constraints_order=["color", "size"],
        blocking_rules={"Add to Bag": ["size", "color"]},
        confidence=0.85,
        source_url="https://zalando.com/example"
    )
    
    # Test to_dict/from_dict
    data = strategy.to_dict()
    assert data["sequence"] == ["Search", "Select Item", "Choose Size", "Add to Bag"]
    assert data["blocking_rules"]["Add to Bag"] == ["size", "color"]
    
    strategy2 = StrategyHint.from_dict(data)
    assert strategy2.sequence == strategy.sequence
    assert strategy2.blocking_rules == strategy.blocking_rules
    print("✅ StrategyHint serialization works")
    
    return strategy


def test_visual_hint():
    """Test VisualHint dataclass."""
    print("\n=== Test: VisualHint ===")
    
    hint = VisualHint(
        selector="#size-picker",
        element_type="dropdown",
        text_pattern="Choose size",
        zone="product_card",
        confidence=0.92
    )
    
    # Test to_dict/from_dict
    data = hint.to_dict()
    assert data["selector"] == "#size-picker"
    assert data["element_type"] == "dropdown"
    
    hint2 = VisualHint.from_dict(data)
    assert hint2.selector == hint.selector
    assert hint2.confidence == hint.confidence
    print("✅ VisualHint serialization works")
    
    return hint


def test_hive_response():
    """Test HiveResponse dataclass."""
    print("\n=== Test: HiveResponse ===")
    
    strategy = StrategyHint(
        sequence=["Search", "Add to Cart"],
        constraints_order=["size"],
        blocking_rules={"Add to Cart": ["size"]},
        confidence=0.85,
        source_url="https://example.com"
    )
    
    hint = VisualHint(
        selector="#add-to-cart",
        element_type="button",
        text_pattern=None,
        zone="product_card",
        confidence=0.9
    )
    
    response = HiveResponse(
        strategy=strategy,
        visual_hints=[hint],
        cached=False,
        query_time_ms=45
    )
    
    # Test to_dict/from_dict
    data = response.to_dict()
    assert data["strategy"] is not None
    assert len(data["visual_hints"]) == 1
    
    response2 = HiveResponse.from_dict(data)
    assert response2.strategy.sequence == response.strategy.sequence
    assert len(response2.visual_hints) == len(response.visual_hints)
    print("✅ HiveResponse serialization works")


def test_graph_node():
    """Test GraphNode dataclass."""
    print("\n=== Test: GraphNode ===")
    
    node = GraphNode(
        id="tara-abc123",
        tag="button",
        text="Add to Cart",
        role="button",
        zone="product_card",
        interactive=True,
        visible=True,
        rect={"x": 100, "y": 200, "w": 120, "h": 40},
        parent_id="tara-xyz789",
        depth=3,
        state="",
        aria_selected=None,
        aria_expanded=None,
        timestamp=time.time()
    )
    
    # Test to_redis_dict
    redis_dict = node.to_redis_dict()
    assert redis_dict["id"] == "tara-abc123"
    assert redis_dict["tag"] == "button"
    assert redis_dict["interactive"] is True
    
    # Test to_json/from_json
    json_str = node.to_json()
    node2 = GraphNode.from_json(json_str)
    assert node2.id == node.id
    assert node2.text == node.text
    print("✅ GraphNode serialization works")
    
    return node


def test_constraint():
    """Test Constraint dataclass."""
    print("\n=== Test: Constraint ===")
    
    constraint = Constraint(
        name="size",
        value="medium",
        status=ConstraintStatus.FILLED
    )
    
    assert constraint.status == ConstraintStatus.FILLED
    assert constraint.value == "medium"
    
    # Test to_dict/from_dict
    data = constraint.to_dict()
    assert data["status"] == "FILLED"
    
    constraint2 = Constraint.from_dict(data)
    assert constraint2.name == constraint.name
    assert constraint2.status == constraint.status
    print("✅ Constraint serialization works")
    
    return constraint


def test_mission_state():
    """Test MissionState dataclass."""
    print("\n=== Test: MissionState ===")
    
    schema = TacticalSchema(
        action=ActionIntent.PURCHASE,
        target_entity="shirt",
        domain="zalando.com",
        constraints={"size": None, "color": "white"},
        raw_utterance="Buy a white shirt"
    )
    
    size_constraint = Constraint(
        name="size",
        value=None,
        status=ConstraintStatus.MISSING
    )
    
    mission = MissionState(
        mission_id="mission-123",
        session_id="session-456",
        schema=schema,
        status="in_progress",
        current_subgoal_index=0,
        subgoals=["Navigate to products", "Select white shirt", "Choose size"],
        constraints={"size": size_constraint},
        visited_urls=["https://zalando.com"],
        action_history=["click_products_nav"],
        ambiguity_count=0,
        created_at=time.time(),
        updated_at=time.time()
    )
    
    # Test to_dict/from_dict
    data = mission.to_dict()
    assert data["mission_id"] == "mission-123"
    assert data["schema"]["action"] == "purchase"
    assert len(data["subgoals"]) == 3
    
    mission2 = MissionState.from_dict(data)
    assert mission2.mission_id == mission.mission_id
    assert mission2.status == mission.status
    assert len(mission2.subgoals) == len(mission.subgoals)
    print("✅ MissionState serialization works")
    
    # Test to_json/from_json
    json_str = mission.to_json()
    mission3 = MissionState.from_json(json_str)
    assert mission3.mission_id == mission.mission_id
    print("✅ MissionState JSON serialization works")
    
    return mission


def test_scored_candidate():
    """Test ScoredCandidate dataclass."""
    print("\n=== Test: ScoredCandidate ===")
    
    hint = VisualHint(
        selector="#add-to-cart",
        element_type="button",
        text_pattern=None,
        zone="product_card",
        confidence=0.9
    )
    
    candidate = ScoredCandidate(
        node_id="tara-abc123",
        text="Add to Cart",
        tag="button",
        zone="product_card",
        semantic_score=0.85,
        hive_score=0.9,
        hybrid_score=0.87,
        matched_hint=hint,
        reasons=["text_match", "zone_match", "hint_selector_match"]
    )
    
    # Test to_dict/from_dict
    data = candidate.to_dict()
    assert data["hybrid_score"] == 0.87
    assert len(data["reasons"]) == 3
    
    candidate2 = ScoredCandidate.from_dict(data)
    assert candidate2.node_id == candidate.node_id
    assert candidate2.hybrid_score == candidate.hybrid_score
    print("✅ ScoredCandidate serialization works")
    
    return candidate


def test_detective_report():
    """Test DetectiveReport dataclass."""
    print("\n=== Test: DetectiveReport ===")
    
    candidate = ScoredCandidate(
        node_id="tara-abc123",
        text="Add to Cart",
        tag="button",
        zone="product_card",
        semantic_score=0.85,
        hive_score=0.9,
        hybrid_score=0.87,
        matched_hint=None,
        reasons=["text_match"]
    )
    
    report = DetectiveReport(
        candidates=[candidate],
        best_match=candidate,
        is_ambiguous=False,
        ambiguous_count=1,
        has_obstacle=False,
        obstacle_type=None,
        dismiss_button_id=None,
        page_type="product",
        recommended_action="click",
        confidence="high"
    )
    
    # Test to_dict/from_dict
    data = report.to_dict()
    assert data["best_match"] is not None
    assert data["confidence"] == "high"
    
    report2 = DetectiveReport.from_dict(data)
    assert report2.best_match.node_id == report.best_match.node_id
    assert report2.confidence == report.confidence
    print("✅ DetectiveReport serialization works")


def test_dom_delta():
    """Test DomDelta dataclass."""
    print("\n=== Test: DomDelta ===")
    
    node = GraphNode(
        id="tara-new123",
        tag="button",
        text="Buy Now",
        role="button",
        zone="main",
        interactive=True,
        visible=True,
        rect={"x": 50, "y": 100, "w": 80, "h": 30},
        parent_id=None,
        depth=2,
        state="",
        aria_selected=None,
        aria_expanded=None,
        timestamp=time.time()
    )
    
    delta = DomDelta(
        delta_type="add",
        nodes=[node],
        removed_ids=[],
        url="https://shop.com/products",
        timestamp=time.time()
    )
    
    # Test to_dict/from_dict
    data = delta.to_dict()
    assert data["delta_type"] == "add"
    assert len(data["nodes"]) == 1
    
    delta2 = DomDelta.from_dict(data)
    assert delta2.delta_type == delta.delta_type
    assert len(delta2.nodes) == len(delta.nodes)
    print("✅ DomDelta serialization works")
    
    # Test to_json/from_json
    json_str = delta.to_json()
    delta3 = DomDelta.from_json(json_str)
    assert delta3.url == delta.url
    print("✅ DomDelta JSON serialization works")


def test_utility_functions():
    """Test generic serialize/deserialize helpers."""
    print("\n=== Test: Utility Functions ===")
    
    schema = TacticalSchema(
        action=ActionIntent.SEARCH,
        target_entity="shoes",
        domain="shop.com",
        constraints={"size": "10"},
        raw_utterance="Find size 10 shoes"
    )
    
    # Test serialize_for_redis
    json_str = serialize_for_redis(schema)
    assert isinstance(json_str, str)
    
    # Test deserialize_from_json
    schema2 = deserialize_from_json(json_str, TacticalSchema)
    assert schema2.action == schema.action
    assert schema2.target_entity == schema.target_entity
    print("✅ Utility functions work")


def test_integration_example():
    """Integration example showing full flow."""
    print("\n=== Integration Example ===")
    
    # 1. User input → TacticalSchema (Mind Reader output)
    schema = TacticalSchema(
        action=ActionIntent.PURCHASE,
        target_entity="white shirt",
        domain="zalando.com",
        constraints={"color": "white", "size": None},
        raw_utterance="Buy a white shirt"
    )
    print(f"1. Mind Reader output: {schema.to_query_string()}")
    print(f"   Missing constraints: {schema.missing_constraints()}")
    
    # 2. Hive Response (Strategy + Visual Hints)
    strategy = StrategyHint(
        sequence=["Search", "Select Color", "Choose Size", "Add to Bag"],
        constraints_order=["color", "size"],
        blocking_rules={"Add to Bag": ["size", "color"]},
        confidence=0.85,
        source_url="https://zalando.com/shirts"
    )
    
    visual_hint = VisualHint(
        selector="#add-to-bag",
        element_type="button",
        text_pattern="Add to.*Bag",
        zone="product_card",
        confidence=0.92
    )
    
    hive_response = HiveResponse(
        strategy=strategy,
        visual_hints=[visual_hint],
        cached=False,
        query_time_ms=45
    )
    print(f"2. Hive Response: Strategy={len(strategy.sequence)} steps, Hints={len(hive_response.visual_hints)}")
    
    # 3. Graph Node (DOM element)
    node = GraphNode(
        id="tara-abc123",
        tag="button",
        text="Add to Bag",
        role="button",
        zone="product_card",
        interactive=True,
        visible=True,
        rect={"x": 100, "y": 200, "w": 120, "h": 40},
        parent_id=None,
        depth=3,
        state="",
        aria_selected=None,
        aria_expanded=None,
        timestamp=time.time()
    )
    print(f"3. Graph Node: {node.id} - '{node.text}'")
    
    # 4. Detective Report
    candidate = ScoredCandidate(
        node_id=node.id,
        text=node.text,
        tag=node.tag,
        zone=node.zone,
        semantic_score=0.85,
        hive_score=0.92,
        hybrid_score=0.88,
        matched_hint=visual_hint,
        reasons=["text_match", "zone_match", "hint_match"]
    )
    
    report = DetectiveReport(
        candidates=[candidate],
        best_match=candidate,
        is_ambiguous=False,
        ambiguous_count=1,
        has_obstacle=False,
        obstacle_type=None,
        dismiss_button_id=None,
        page_type="product",
        recommended_action="click",
        confidence="high"
    )
    print(f"4. Detective Report: Best match '{report.best_match.text}' (score: {report.best_match.hybrid_score})")
    
    # 5. Mission State
    constraint = Constraint(
        name="size",
        value=None,
        status=ConstraintStatus.MISSING
    )
    
    mission = MissionState(
        mission_id="mission-789",
        session_id="session-456",
        schema=schema,
        status="blocked",
        current_subgoal_index=2,
        subgoals=["Search shirts", "Select white", "Choose size"],
        constraints={"size": constraint},
        visited_urls=["https://zalando.com/shirts"],
        action_history=["click_search", "filter_white"],
        ambiguity_count=0,
        created_at=time.time(),
        updated_at=time.time()
    )
    print(f"5. Mission State: status={mission.status}, subgoal={mission.subgoals[mission.current_subgoal_index]}")
    
    print("\n✅ Integration example complete!")


def main():
    """Run all tests."""
    print("=" * 60)
    print("TARA MODELS - UNIT TESTS")
    print("=" * 60)
    
    try:
        # Run all unit tests
        test_action_intent_enum()
        test_tactical_schema()
        test_strategy_hint()
        test_visual_hint()
        test_hive_response()
        test_graph_node()
        test_constraint()
        test_mission_state()
        test_scored_candidate()
        test_detective_report()
        test_dom_delta()
        test_utility_functions()
        
        # Run integration example
        test_integration_example()
        
        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
        return True
        
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        return False
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)

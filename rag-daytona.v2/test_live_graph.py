#!/usr/bin/env python3
"""
test_live_graph.py

Unit tests for live_graph.py (without requiring Redis).
Tests compilation, imports, and class structure.

Run: python test_live_graph.py
"""

import sys
import time
import json

# Test imports
print("=== Testing Imports ===")
try:
    from tara_models import GraphNode, DomDelta
    print("✅ tara_models imports successful")
except ImportError as e:
    print(f"❌ tara_models import failed: {e}")
    sys.exit(1)

try:
    from live_graph import LiveGraph, create_live_graph
    print("✅ live_graph imports successful")
except ImportError as e:
    print(f"❌ live_graph import failed: {e}")
    sys.exit(1)


def test_graph_node_creation():
    """Test creating GraphNode instances."""
    print("\n=== Test: GraphNode Creation ===")
    
    node = GraphNode(
        id="tara-test123",
        tag="button",
        text="Click Me",
        role="button",
        zone="main",
        interactive=True,
        visible=True,
        rect={"x": 100, "y": 200, "w": 80, "h": 40},
        parent_id=None,
        depth=2,
        state="",
        aria_selected=None,
        aria_expanded=None,
        timestamp=time.time()
    )
    
    assert node.id == "tara-test123"
    assert node.tag == "button"
    assert node.text == "Click Me"
    assert node.interactive is True
    assert node.visible is True
    
    # Test serialization
    redis_dict = node.to_redis_dict()
    assert "id" in redis_dict
    assert "tag" in redis_dict
    
    json_str = node.to_json()
    assert isinstance(json_str, str)
    
    # Test deserialization
    node2 = GraphNode.from_json(json_str)
    assert node2.id == node.id
    assert node2.text == node.text
    
    print("✅ GraphNode creation and serialization works")
    return node


def test_dom_delta_creation():
    """Test creating DomDelta instances."""
    print("\n=== Test: DomDelta Creation ===")
    
    node = GraphNode(
        id="tara-new123",
        tag="a",
        text="Link",
        role="link",
        zone="nav",
        interactive=True,
        visible=True,
        rect={"x": 0, "y": 0, "w": 50, "h": 20},
        parent_id=None,
        depth=1,
        state="",
        aria_selected=None,
        aria_expanded=None,
        timestamp=time.time()
    )
    
    delta = DomDelta(
        delta_type="add",
        nodes=[node],
        removed_ids=[],
        url="https://example.com",
        timestamp=time.time()
    )
    
    assert delta.delta_type == "add"
    assert len(delta.nodes) == 1
    assert delta.url == "https://example.com"
    
    # Test serialization
    data = delta.to_dict()
    assert data["delta_type"] == "add"
    assert len(data["nodes"]) == 1
    
    json_str = delta.to_json()
    assert isinstance(json_str, str)
    
    # Test deserialization
    delta2 = DomDelta.from_json(json_str)
    assert delta2.delta_type == delta.delta_type
    assert len(delta2.nodes) == len(delta.nodes)
    
    print("✅ DomDelta creation and serialization works")
    return delta


def test_live_graph_class_structure():
    """Test LiveGraph class structure (without Redis)."""
    print("\n=== Test: LiveGraph Class Structure ===")
    
    # Check class exists and has expected methods
    assert hasattr(LiveGraph, '__init__')
    assert hasattr(LiveGraph, 'ingest_delta')
    assert hasattr(LiveGraph, 'get_all_nodes')
    assert hasattr(LiveGraph, 'get_visible_nodes')
    assert hasattr(LiveGraph, 'get_interactive_nodes')
    assert hasattr(LiveGraph, 'get_nodes_by_zone')
    assert hasattr(LiveGraph, 'find_by_id')
    assert hasattr(LiveGraph, 'find_by_text')
    assert hasattr(LiveGraph, 'get_buttons')
    assert hasattr(LiveGraph, 'get_inputs')
    assert hasattr(LiveGraph, 'get_links')
    assert hasattr(LiveGraph, 'get_stats')
    assert hasattr(LiveGraph, 'clear_graph')
    
    # Check method signatures
    import inspect
    
    # __init__ should have redis_client parameter
    init_sig = inspect.signature(LiveGraph.__init__)
    assert 'redis_client' in init_sig.parameters
    
    # ingest_delta should be async
    assert inspect.iscoroutinefunction(LiveGraph.ingest_delta)
    
    # get_all_nodes should be async
    assert inspect.iscoroutinefunction(LiveGraph.get_all_nodes)
    
    print("✅ LiveGraph class structure is correct")


def test_live_graph_key_methods():
    """Test LiveGraph key generation methods."""
    print("\n=== Test: LiveGraph Key Methods ===")
    
    # Create a mock Redis client
    class MockRedis:
        pass
    
    live_graph = LiveGraph(MockRedis())  # type: ignore
    
    # Test key generation
    graph_key = live_graph._graph_key("session-123")
    assert graph_key == "graph:session-123"
    
    node_key = live_graph._node_key("session-123", "tara-abc")
    assert node_key == "graph:session-123:node:tara-abc"
    
    print("✅ LiveGraph key methods work")


def test_create_live_graph_factory():
    """Test factory function."""
    print("\n=== Test: Factory Function ===")
    
    # Factory should return LiveGraph instance
    # (We can't test actual Redis connection without Redis)
    assert callable(create_live_graph)
    
    print("✅ Factory function exists")


def test_delta_scenarios():
    """Test different delta scenarios."""
    print("\n=== Test: Delta Scenarios ===")
    
    # Scenario 1: Full scan delta
    node1 = GraphNode(
        id="tara-1",
        tag="button",
        text="Button 1",
        role="button",
        zone="main",
        interactive=True,
        visible=True,
        rect={},
        parent_id=None,
        depth=1,
        state="",
        aria_selected=None,
        aria_expanded=None,
        timestamp=time.time()
    )
    
    full_scan_delta = {
        "delta_type": "full_scan",
        "nodes": [node1.to_redis_dict()],
        "url": "https://example.com",
        "timestamp": time.time()
    }
    
    assert full_scan_delta["delta_type"] == "full_scan"
    assert len(full_scan_delta["nodes"]) == 1
    print("✅ Full scan delta structure correct")
    
    # Scenario 2: Incremental update delta
    node2 = GraphNode(
        id="tara-2",
        tag="a",
        text="Link 2",
        role="link",
        zone="nav",
        interactive=True,
        visible=True,
        rect={},
        parent_id=None,
        depth=1,
        state="",
        aria_selected=None,
        aria_expanded=None,
        timestamp=time.time()
    )
    
    update_delta = {
        "delta_type": "update",
        "changes": [
            {"type": "add", "node": node2.to_redis_dict()},
            {"type": "remove", "id": "tara-old"}
        ],
        "url": "https://example.com/new",
        "timestamp": time.time()
    }
    
    assert update_delta["delta_type"] == "update"
    assert len(update_delta["changes"]) == 2
    print("✅ Incremental update delta structure correct")


def integration_example():
    """Integration example showing usage pattern."""
    print("\n=== Integration Example ===")
    
    # 1. Create sample delta (as if from tara_sensor.js)
    button_node = GraphNode(
        id="tara-abc123",
        tag="button",
        text="Add to Cart",
        role="button",
        zone="product_card",
        interactive=True,
        visible=True,
        rect={"x": 100, "y": 200, "w": 120, "h": 40},
        parent_id="tara-xyz",
        depth=3,
        state="",
        aria_selected=None,
        aria_expanded=None,
        timestamp=time.time()
    )
    
    delta = DomDelta(
        delta_type="add",
        nodes=[button_node],
        removed_ids=[],
        url="https://shop.com/product/123",
        timestamp=time.time()
    )
    
    print(f"1. Created delta: {delta.delta_type} with {len(delta.nodes)} nodes")
    print(f"   Node: {delta.nodes[0].text} (id: {delta.nodes[0].id})")
    
    # 2. Serialize for WebSocket transmission
    delta_json = delta.to_json()
    print(f"2. Serialized delta: {len(delta_json)} bytes")
    
    # 3. Deserialize on server
    delta_received = DomDelta.from_json(delta_json)
    print(f"3. Deserialized delta: {delta_received.delta_type}")
    
    # 4. Would ingest to LiveGraph (requires Redis)
    # live_graph = LiveGraph(redis_client)
    # await live_graph.ingest_delta("session-123", delta_received.to_dict())
    
    print("✅ Integration example complete (Redis not required for this test)")


def main():
    """Run all tests."""
    print("=" * 60)
    print("LIVE GRAPH - UNIT TESTS")
    print("=" * 60)
    
    try:
        # Run all unit tests
        test_graph_node_creation()
        test_dom_delta_creation()
        test_live_graph_class_structure()
        test_live_graph_key_methods()
        test_create_live_graph_factory()
        test_delta_scenarios()
        
        # Run integration example
        integration_example()
        
        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
        print("\nNote: These tests verify compilation and structure.")
        print("Full integration tests require Redis running on localhost:6379")
        return True
        
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)

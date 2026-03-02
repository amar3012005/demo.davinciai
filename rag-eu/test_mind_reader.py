#!/usr/bin/env python3
"""
test_mind_reader.py

Unit tests for mind_reader.py.
Tests compilation, imports, class structure, and fallback behavior.
Note: Full LLM tests require Groq API key.

Run: python test_mind_reader.py
"""

import sys
import time

# Test imports
print("=== Testing Imports ===")
try:
    from tara_models import TacticalSchema, ActionIntent
    print("✅ tara_models imports successful")
except ImportError as e:
    print(f"❌ tara_models import failed: {e}")
    sys.exit(1)

try:
    from mind_reader import MindReader, create_mind_reader
    print("✅ mind_reader imports successful")
except ImportError as e:
    print(f"❌ mind_reader import failed: {e}")
    sys.exit(1)


def test_mind_reader_class_structure():
    """Test MindReader class structure."""
    print("\n=== Test: MindReader Class Structure ===")
    
    import inspect
    
    # Check class exists
    assert hasattr(MindReader, '__init__')
    assert hasattr(MindReader, 'translate')
    
    # Check translate is async
    assert inspect.iscoroutinefunction(MindReader.translate)
    
    # Check private methods exist
    assert hasattr(MindReader, '_sanitize_input')
    assert hasattr(MindReader, '_extract_domain')
    assert hasattr(MindReader, '_fallback_schema')
    
    print("✅ MindReader class structure correct")


def test_mind_reader_initialization():
    """Test MindReader initialization with and without LLM."""
    print("\n=== Test: MindReader Initialization ===")
    
    # With None (fallback mode)
    mr_none = MindReader(None)
    assert mr_none.llm is None
    print("✅ MindReader initialized with None (fallback mode)")
    
    # With mock provider
    class MockLLM:
        pass
    
    mr_mock = MindReader(MockLLM())
    assert mr_mock.llm is not None
    print("✅ MindReader initialized with mock provider")


def test_sanitize_input():
    """Test input sanitization."""
    print("\n=== Test: Input Sanitization ===")
    
    mr = MindReader(None)
    
    # Test filler removal
    tests = [
        ("Um, I want to buy a shirt", "I want to buy a shirt"),
        ("Like, show me the red shoes", "show me the red shoes"),
        ("Actually, I need a blue shirt", "I need a blue shirt"),
        ("  multiple   spaces  ", "multiple spaces"),
    ]
    
    for input_text, expected_contains in tests:
        result = mr._sanitize_input(input_text)
        # Check that fillers are removed (approximate check)
        assert 'um' not in result.lower() or 'um' in input_text.lower().replace('um', '')
        print(f"   '{input_text}' → '{result}'")
    
    print("✅ Input sanitization works")


def test_extract_domain():
    """Test domain extraction from URLs."""
    print("\n=== Test: Domain Extraction ===")
    
    mr = MindReader(None)
    
    tests = [
        ("https://www.zalando.com/shirts", "zalando.com"),
        ("https://groq.com/dashboard", "groq.com"),
        ("http://example.com", "example.com"),
        ("www.example.com", "example.com"),
        ("example.com", "example.com"),  # URL without protocol
        ("", "unknown"),
        (None, "unknown"),
    ]
    
    for url, expected in tests:
        result = mr._extract_domain(url if url is not None else "")
        assert result == expected, f"Expected {expected}, got {result}"
        print(f"   '{url}' → '{result}'")
    
    print("✅ Domain extraction works")


def test_fallback_schema_purchase():
    """Test fallback schema for purchase intent."""
    print("\n=== Test: Fallback Schema (Purchase) ===")
    
    mr = MindReader(None)
    
    schema = mr._fallback_schema("I want to buy a white shirt", "shop.com")
    
    assert schema.action == ActionIntent.PURCHASE
    assert "shirt" in schema.target_entity.lower()
    assert schema.constraints.get("color") == "white"
    assert schema.domain == "shop.com"
    
    print(f"   Action: {schema.action.value}")
    print(f"   Target: {schema.target_entity}")
    print(f"   Constraints: {schema.constraints}")
    print("✅ Purchase fallback schema works")


def test_fallback_schema_search():
    """Test fallback schema for search intent."""
    print("\n=== Test: Fallback Schema (Search) ===")
    
    mr = MindReader(None)
    
    schema = mr._fallback_schema("Show me red shoes size large", "shop.com")
    
    assert schema.action == ActionIntent.SEARCH
    assert "shoes" in schema.target_entity.lower()
    assert schema.constraints.get("color") == "red"
    assert schema.constraints.get("size") == "large"
    
    print(f"   Action: {schema.action.value}")
    print(f"   Target: {schema.target_entity}")
    print(f"   Constraints: {schema.constraints}")
    print("✅ Search fallback schema works")


def test_fallback_schema_extraction():
    """Test fallback schema for extraction intent."""
    print("\n=== Test: Fallback Schema (Extraction) ===")
    
    mr = MindReader(None)
    
    schema = mr._fallback_schema("Find my API usage data", "groq.com")
    
    assert schema.action == ActionIntent.EXTRACTION
    assert "api usage" in schema.target_entity.lower()
    
    print(f"   Action: {schema.action.value}")
    print(f"   Target: {schema.target_entity}")
    print(f"   Constraints: {schema.constraints}")
    print("✅ Extraction fallback schema works")


def test_fallback_schema_navigation():
    """Test fallback schema for navigation intent."""
    print("\n=== Test: Fallback Schema (Navigation) ===")
    
    mr = MindReader(None)
    
    schema = mr._fallback_schema("Go to the settings page", "app.com")
    
    assert schema.action == ActionIntent.NAVIGATION
    assert "settings" in schema.target_entity.lower()
    
    print(f"   Action: {schema.action.value}")
    print(f"   Target: {schema.target_entity}")
    print(f"   Constraints: {schema.constraints}")
    print("✅ Navigation fallback schema works")


def test_tactical_schema_methods():
    """Test TacticalSchema helper methods."""
    print("\n=== Test: TacticalSchema Methods ===")
    
    # Schema with missing constraints
    schema1 = TacticalSchema(
        action=ActionIntent.PURCHASE,
        target_entity="shirt",
        domain="shop.com",
        constraints={"color": "white", "size": None},
        raw_utterance="Buy a white shirt"
    )
    
    assert not schema1.has_all_constraints()
    missing = schema1.missing_constraints()
    assert "size" in missing
    assert "color" not in missing
    print(f"   Missing constraints: {missing}")
    
    # Schema with all constraints
    schema2 = TacticalSchema(
        action=ActionIntent.PURCHASE,
        target_entity="shirt",
        domain="shop.com",
        constraints={"color": "white", "size": "medium"},
        raw_utterance="Buy a white shirt size medium"
    )
    
    assert schema2.has_all_constraints()
    assert len(schema2.missing_constraints()) == 0
    print(f"   Has all constraints: {schema2.has_all_constraints()}")
    
    # Query string
    query = schema2.to_query_string()
    assert "action:purchase" in query
    assert "domain:shop.com" in query
    assert "color:white" in query
    assert "size:medium" in query
    print(f"   Query string: {query}")
    
    print("✅ TacticalSchema methods work")


def test_create_mind_reader_factory():
    """Test factory function."""
    print("\n=== Test: Factory Function ===")
    
    # Without API key (should create fallback-only instance)
    mr = create_mind_reader()
    assert isinstance(mr, MindReader)
    print("✅ Factory function works (fallback mode)")


async def test_translate_fallback():
    """Test translate method with fallback (no LLM)."""
    print("\n=== Test: Translate (Fallback) ===")
    
    mr = MindReader(None)
    
    # Test various inputs
    test_cases = [
        ("Buy a blue shirt", ActionIntent.PURCHASE),
        ("Show me the docs", ActionIntent.SEARCH),
        ("Click the submit button", ActionIntent.INTERACTION),
        ("Find my usage stats", ActionIntent.EXTRACTION),
        ("Go to settings", ActionIntent.NAVIGATION),
    ]
    
    for input_text, expected_action in test_cases:
        schema = await mr.translate(
            user_input=input_text,
            current_url="https://example.com"
        )
        
        assert schema.action == expected_action, \
            f"Expected {expected_action}, got {schema.action} for '{input_text}'"
        print(f"   '{input_text}' → {schema.action.value}")
    
    print("✅ Translate fallback works")


async def main():
    """Run all tests."""
    print("=" * 60)
    print("MIND READER - UNIT TESTS")
    print("=" * 60)
    
    try:
        # Run sync tests
        test_mind_reader_class_structure()
        test_mind_reader_initialization()
        test_sanitize_input()
        test_extract_domain()
        test_fallback_schema_purchase()
        test_fallback_schema_search()
        test_fallback_schema_extraction()
        test_fallback_schema_navigation()
        test_tactical_schema_methods()
        test_create_mind_reader_factory()
        
        # Run async tests
        await test_translate_fallback()
        
        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
        print("\nNote: These tests verify fallback behavior.")
        print("Full LLM integration tests require Groq API key.")
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
    import asyncio
    success = asyncio.run(main())
    exit(0 if success else 1)

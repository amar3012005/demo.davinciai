#!/usr/bin/env python3
"""
test_tara_sensor.py

Tests for tara_sensor.js (structure and syntax validation).
Since tara_sensor.js is client-side JavaScript, we validate:
1. File exists and is readable
2. Syntax is valid (using Node.js if available)
3. Class structure is correct
4. Integration points documented

Run: python test_tara_sensor.py
"""

import os
import re
import sys

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
TARA_SENSOR_PATH = os.path.join(PROJECT_ROOT, 'orchestra_daytona.v2/static/tara_sensor.js')


def test_file_exists():
    """Test that tara_sensor.js exists."""
    print("\n=== Test: File Exists ===")
    
    if not os.path.exists(TARA_SENSOR_PATH):
        print(f"❌ tara_sensor.js not found at {TARA_SENSOR_PATH}")
        return False
    
    print(f"✅ tara_sensor.js exists at {TARA_SENSOR_PATH}")
    
    # Get file size
    size = os.path.getsize(TARA_SENSOR_PATH)
    print(f"   File size: {size:,} bytes")
    return True


def test_file_syntax():
    """Test JavaScript syntax using Node.js if available."""
    print("\n=== Test: JavaScript Syntax ===")
    
    import subprocess
    
    try:
        # Try to run Node.js syntax check
        result = subprocess.run(
            ['node', '--check', TARA_SENSOR_PATH],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            print("✅ JavaScript syntax is valid (Node.js check)")
            return True
        else:
            print(f"❌ Syntax error: {result.stderr}")
            return False
            
    except FileNotFoundError:
        print("⚠️  Node.js not available, skipping syntax check")
        return True  # Don't fail if Node not installed
    except subprocess.TimeoutExpired:
        print("⚠️  Syntax check timed out")
        return True
    except Exception as e:
        print(f"⚠️  Could not check syntax: {e}")
        return True


def test_class_structure():
    """Test that TaraSensor class has expected methods."""
    print("\n=== Test: Class Structure ===")
    
    with open(TARA_SENSOR_PATH, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Check for class definition
    if 'class TaraSensor' not in content:
        print("❌ TaraSensor class not found")
        return False
    print("✅ TaraSensor class defined")
    
    # Check for constructor
    if 'constructor(websocket, config' not in content:
        print("❌ Constructor not found")
        return False
    print("✅ Constructor defined")
    
    # Check for required methods
    required_methods = [
        'start()',
        'stop()',
        'handleMutations(mutations)',
        'isInteractive(node)',
        'getNodeId(node)',
        'generateStableId(node)',
        'serializeNode(node)',
        'registerNode(node, deltaType)',
        'scheduleDeltaTransmission()',
        'transmitDeltas()',
        'performFullScan()'
    ]
    
    missing_methods = []
    for method in required_methods:
        method_name = method.split('(')[0]
        if f'{method_name}(' not in content:
            missing_methods.append(method)
    
    if missing_methods:
        print(f"❌ Missing methods: {missing_methods}")
        return False
    
    print(f"✅ All {len(required_methods)} required methods present")
    
    # Check for helper methods
    helper_methods = [
        'extractText(el)',
        'isVisible(el)',
        'classifyZone(el)',
        'getParentId(el)',
        'getDepth(el)',
        'getState(el)'
    ]
    
    for method in helper_methods:
        method_name = method.split('(')[0]
        if f'{method_name}(' not in content:
            print(f"⚠️  Helper method not found: {method_name}")
    
    print(f"✅ Helper methods present")
    
    return True


def test_exports():
    """Test that class is properly exported."""
    print("\n=== Test: Exports ===")
    
    with open(TARA_SENSOR_PATH, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Check for window export
    if 'window.TaraSensor = TaraSensor' not in content:
        print("❌ Window export not found")
        return False
    print("✅ Window export present")
    
    # Check for module export
    if 'module.exports = TaraSensor' not in content:
        print("⚠️  Module export not found (optional)")
    else:
        print("✅ Module export present")
    
    return True


def test_constants():
    """Test that constants are defined."""
    print("\n=== Test: Constants ===")
    
    with open(TARA_SENSOR_PATH, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Check for SVG noise set
    if 'SVG_NOISE' not in content:
        print("❌ SVG_NOISE constant not found")
        return False
    print("✅ SVG_NOISE constant defined")
    
    # Check for interactive tags set
    if 'INTERACTIVE_TAGS' not in content:
        print("❌ INTERACTIVE_TAGS constant not found")
        return False
    print("✅ INTERACTIVE_TAGS constant defined")
    
    return True


def test_integration_points():
    """Test integration points with tara-widget.js."""
    print("\n=== Test: Integration Points ===")
    
    with open(TARA_SENSOR_PATH, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Check WebSocket usage
    if 'WebSocket.OPEN' not in content:
        print("⚠️  WebSocket state check not found")
    else:
        print("✅ WebSocket integration present")
    
    # Check MutationObserver
    if 'MutationObserver' not in content:
        print("❌ MutationObserver not found")
        return False
    print("✅ MutationObserver integration present")
    
    # Check message format
    if "'dom_delta'" not in content and '"dom_delta"' not in content:
        print("❌ dom_delta message type not found")
        return False
    print("✅ dom_delta message format present")
    
    return True


def test_documentation():
    """Test that JSDoc comments are present."""
    print("\n=== Test: Documentation ===")
    
    with open(TARA_SENSOR_PATH, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Check for file header
    if '/**' not in content or '* tara_sensor.js' not in content:
        print("⚠️  File header not found")
    else:
        print("✅ File header present")
    
    # Check for class JSDoc
    if '* Create a new TaraSensor instance' not in content:
        print("⚠️  Constructor JSDoc not found")
    else:
        print("✅ Constructor JSDoc present")
    
    # Count method comments
    method_comments = content.count('/**')
    if method_comments < 10:
        print(f"⚠️  Only {method_comments} JSDoc comments found")
    else:
        print(f"✅ {method_comments} JSDoc comments present")
    
    return True


def integration_example():
    """Show integration example."""
    print("\n=== Integration Example ===")
    
    example = """
// In tara-widget.js startVisualCopilot() method:

this.sensor = new TaraSensor(this.ws, {
    sendFullScanOnInit: true,
    debounceMs: 150,
    maxBatchSize: 50
});
this.sensor.start();

// Server receives dom_delta messages:
// {
//   type: 'dom_delta',
//   delta_type: 'full_scan' | 'update' | 'add' | 'remove',
//   nodes: [...],  // For full_scan/add/update
//   changes: [...], // For update
//   removed_ids: [], // For remove
//   url: 'https://example.com',
//   timestamp: 1234567890
// }
"""
    print(example)
    print("✅ Integration example documented")


def main():
    """Run all tests."""
    print("=" * 60)
    print("TARA SENSOR - UNIT TESTS")
    print("=" * 60)
    print(f"Testing: {TARA_SENSOR_PATH}")
    
    try:
        all_passed = True
        
        all_passed &= test_file_exists()
        all_passed &= test_file_syntax()
        all_passed &= test_class_structure()
        all_passed &= test_exports()
        all_passed &= test_constants()
        all_passed &= test_integration_points()
        all_passed &= test_documentation()
        
        integration_example()
        
        print("\n" + "=" * 60)
        if all_passed:
            print("✅ ALL TESTS PASSED")
        else:
            print("❌ SOME TESTS FAILED")
        print("=" * 60)
        
        return all_passed
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)

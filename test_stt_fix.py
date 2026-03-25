#!/usr/bin/env python3
"""
Quick test to verify STT rejection logic is working correctly.
Tests that common greetings are accepted and confidence metrics are handled gracefully.
"""

import sys
sys.path.insert(0, '/Users/amar/demo.davinciai/Orchestrator-eu')

from core.ws_handler import OrchestratorWSHandler
from unittest.mock import Mock

# Create mock session and handler
mock_session = Mock()
handler = OrchestratorWSHandler(None, None, None)

# Test cases: (text, data_dict, should_reject, description)
test_cases = [
    ("Hello!", {}, False, "Greeting with no metrics should be accepted"),
    ("hello", {}, False, "Lowercase greeting should be accepted"),
    ("Hi there", {}, False, "Multi-word greeting should be accepted"),
    ("Hello", {"no_speech_prob": None, "avg_logprob": None}, False, "Greeting with null metrics should be accepted"),
    ("This is a longer sentence", {}, False, "Normal sentence with no metrics should be accepted"),
    ("yes", {}, False, "Single word greeting allowed"),
    ("random word", {"no_speech_prob": 0.8}, True, "Non-greeting with high no_speech_prob should be rejected"),
    ("a", {}, True, "Single character should be rejected"),
    ("", {}, True, "Empty text should be rejected"),
    ("Thank you for watching", {}, True, "Hallucination phrase should be rejected"),
    ("aaaa", {}, True, "Repeated character should be rejected"),
]

print("=" * 70)
print("STT REJECTION LOGIC TEST SUITE")
print("=" * 70)

passed = 0
failed = 0

for text, data, expected_reject, description in test_cases:
    result = handler._should_reject_stt_transcript(mock_session, text, data)
    status = "✅" if result == expected_reject else "❌"

    if result == expected_reject:
        passed += 1
    else:
        failed += 1

    action = "REJECT" if result else "ACCEPT"
    expected_action = "REJECT" if expected_reject else "ACCEPT"

    print(f"\n{status} {description}")
    print(f"   Text: '{text[:40]}{'...' if len(text) > 40 else ''}'")
    print(f"   Result: {action} (expected: {expected_action})")
    if data:
        print(f"   Data: {data}")

print("\n" + "=" * 70)
print(f"RESULTS: {passed} passed, {failed} failed out of {len(test_cases)} tests")
print("=" * 70)

sys.exit(0 if failed == 0 else 1)

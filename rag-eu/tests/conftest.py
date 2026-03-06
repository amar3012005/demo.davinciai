"""Pytest configuration and fixtures for FSM routing tests."""

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

# Add rag-eu root directory to path for imports
TESTS_DIR = Path(__file__).resolve().parent
RAG_EU_DIR = TESTS_DIR.parent
if str(RAG_EU_DIR) not in sys.path:
    sys.path.insert(0, str(RAG_EU_DIR))


def pytest_configure(config):
    # Keep local test runs independent from optional pytest-asyncio plugin.
    config.addinivalue_line("markers", "asyncio: mark test as async")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    test_fn = pyfuncitem.obj
    if inspect.iscoroutinefunction(test_fn):
        asyncio.run(test_fn(**pyfuncitem.funcargs))
        return True
    return None


@pytest.fixture
def default_fsm_context():
    """Default FSM context for testing"""
    return {
        "active": True,
        "pending_field": "name",
        "collected_data": {},
        "retry_counts": {},
        "schema": {
            "fields": {
                "name": {
                    "required": True,
                    "min_length": 2,
                    "max_length": 50,
                    "validation_regex": r"^[a-zA-Z\s\-']+$"
                },
                "email": {
                    "required": True,
                    "validation_regex": r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
                },
                "topic": {
                    "required": True,
                    "min_length": 5,
                    "max_length": 500
                }
            },
            "cancel_keywords": ["cancel", "stop", "nevermind", "forget it", "quit", "exit"],
            "max_retries": 3
        }
    }

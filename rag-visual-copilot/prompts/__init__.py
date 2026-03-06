"""
Prompts package for RAG service.

This module re-exports prompt_manager from the parent prompts.py module.
The prompts.py file and prompts/ directory both exist for backward compatibility.
"""

import sys
import importlib.util
import os

# Load prompts.py as a module (it's shadowed by this prompts/ package)
_prompts_py_path = os.path.join(os.path.dirname(__file__), '..', 'prompts.py')
if os.path.exists(_prompts_py_path):
    # Load the prompts.py file dynamically
    _spec = importlib.util.spec_from_file_location("_prompts_py", _prompts_py_path)
    _prompts_py = importlib.util.module_from_spec(_spec)
    sys.modules["_prompts_py"] = _prompts_py
    _spec.loader.exec_module(_prompts_py)
    
    # Export prompt_manager
    prompt_manager = _prompts_py.prompt_manager
    PromptManager = _prompts_py.PromptManager
    PromptTemplate = _prompts_py.PromptTemplate
else:
    raise ImportError(f"Cannot find prompts.py at {_prompts_py_path}")

__all__ = ["prompt_manager", "PromptManager", "PromptTemplate"]

"""
Tool Specifications for the TARA Compound Last-Mile Execution Agent.

These schemas define the specific rigid capabilities the Groq LLM can invoke
when attempting to autonomously resolve a goal in the `last_mile` phase.

Tool Priority (enforced in system prompt):
  1. complete_mission — when evidence exists
  2. read_page_content — to re-examine visible content
  3. type_text — to search/filter
  4. click_element — only when evidence insufficient
  5. scroll_page — to reveal hidden content
  6. wait_for_ui — when page is loading
  7. request_vision — system-triggered only
"""

LAST_MILE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "complete_mission",
            "description": (
                "HIGHEST PRIORITY TOOL. Finalize the task with a grounded answer. "
                "Call this FIRST whenever the readable page content contains evidence "
                "that answers the user's goal. You MUST include the actual answer text "
                "extracted from the page, not just a summary. Include evidence_refs "
                "pointing to the specific content that supports your answer. "
                "STRICT VERIFICATION: Before calling complete_mission, you MUST verify that the data matches the specific target entity requested. If the user asked for a specific model (e.g. 'Whisper') but the screen shows a different one (e.g. 'GPT-OSS-120B' or 'Playground'), you are NOT done. Use scroll_page or click tabs to find the correct entity entry."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["success", "impossible"],
                        "description": "Whether you successfully found the answer or it is genuinely impossible."
                    },
                    "response": {
                        "type": "string",
                        "description": "The complete grounded answer extracted from visible page content. Must be specific and factual, not vague."
                    },
                    "evidence_refs": {
                        "type": "string",
                        "description": "Specific text snippets or element references from the page that support your answer."
                    },
                    "answer_confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "Confidence level based on how directly the evidence answers the goal."
                    }
                },
                "required": ["status", "response"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_page_content",
            "description": (
                "Re-examine the current visible page content, optionally filtered by focus terms. "
                "Use this BEFORE clicking to check if the answer is already on the page. "
                "Non-terminal: returns content for analysis, does not navigate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "description": "Optional focus terms to filter relevant content (e.g., 'pricing plans' or 'API rate limits')."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Input text into a field and optionally press enter. Use for search/filter when current content doesn't answer the goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "The exact ID of the input element."
                    },
                    "text": {
                        "type": "string",
                        "description": "The exact text payload to type."
                    },
                    "press_enter": {
                        "type": "boolean",
                        "description": "Whether to hit Enter after typing.",
                        "default": True
                    }
                },
                "required": ["target_id", "text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "click_element",
            "description": (
                "Click a specific interactive element using its LiveGraph ID. "
                "ONLY use this when current page content is genuinely insufficient "
                "to answer the goal. You MUST explain in 'why' field what specific "
                "evidence is missing and why this click will provide it. "
                "DO NOT click concept-neighbor links (e.g., if 'API Reference' didn't help, "
                "don't click 'API Documentation' or 'Developer Guide')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "The exact ID (e.g., 't-123') from the DOM list."
                    },
                    "why": {
                        "type": "string",
                        "description": "REQUIRED: What specific evidence is missing from current page AND what you expect this click to reveal."
                    },
                    "force_click": {
                        "type": "boolean",
                        "description": "MUST set to True when clicking ANY link inside a dropdown, menu, popup, or overlay to bypass cursor animations that might accidentally close the menu. Default False."
                    }
                },
                "required": ["target_id", "why"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scroll_page",
            "description": "Scroll the view to reveal more content below or above the fold. Use when you suspect relevant content exists but isn't visible.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["down", "up"],
                        "default": "down"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for_ui",
            "description": "Wait briefly for asynchronous loading to finish. Only use when the page shows loading indicators.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "description": "Number of seconds to wait (1-3 max).",
                        "default": 2
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "request_vision",
            "description": (
                "DO NOT call this proactively. This tool is triggered by the system "
                "when the text DOM is ambiguous after multiple failed attempts. "
                "If you need to understand visual elements, call this only when "
                "explicitly instructed by a system message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "What specific visual cue or context you are looking for."
                    }
                },
                "required": ["reason"]
            }
        }
    },
]

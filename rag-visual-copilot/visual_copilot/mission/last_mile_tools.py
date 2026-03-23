"""
Tool Specifications for the TARA Compound Last-Mile Execution Agent.

NOTE: This is the legacy native tool-calling path.
The one-call JSON reasoning-action path is now preferred when LAST_MILE_ONECALL_REASONING_ACTION_ENABLED is enabled.

These schemas define the specific rigid capabilities the Groq LLM can invoke
when attempting to autonomously resolve a goal in the `last_mile` phase.

Tool Priority (enforced in system prompt):
  For ACTION goals (Create/Add/Click/Select/Open/Make):
    1. click_element — MUST click the target button first
    2. wait_for_ui — wait for UI to update after click
    3. complete_mission — only AFTER the click succeeds and result is visible
  For INFORMATION goals (What/How/Show/Find):
    1. complete_mission — when evidence exists in readable content
    2. read_page_content — to re-examine visible content
    3. click_element — only when evidence insufficient
  Common:
    4. type_text — to search/filter
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
                "Finalize the task with a grounded answer extracted from visible page content. "
                "CRITICAL CONSTRAINT: If the user's goal is action-oriented (Create, Add, Click, Select, Open, Make), "
                "you are FORBIDDEN from calling complete_mission until you have physically CLICKED the relevant button "
                "and verified the resulting UI change. Seeing a button is NOT completion — clicking it IS. "
                "For information-seeking goals (What, How, Show usage), call this when readable content answers the goal. "
                "You MUST include actual answer text and evidence_refs from the page."
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
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": (
                "Escalate to human user when blocked or clarification is needed. "
                "Use this when you encounter: missing required information, "
                "ambiguous options requiring user choice, CAPTCHA or human verification, "
                "unsupported page formats (PDF, Canvas), or when stuck after multiple attempts. "
                "Provide a clear explanation and specific question for the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "blockage_type": {
                        "type": "string",
                        "enum": ["information_wall", "ambiguity_wall", "verification_wall", "captcha_wall", "architecture_wall", "unknown"],
                        "description": "Type of blockage requiring escalation"
                    },
                    "speech": {
                        "type": "string",
                        "description": "Human-friendly explanation of the situation"
                    },
                    "ask": {
                        "type": "string",
                        "description": "Specific question for the user"
                    },
                    "diagnostics": {
                        "type": "object",
                        "description": "Diagnostic information for debugging"
                    },
                    "resume_context": {
                        "type": "object",
                        "description": "Context for resuming mission"
                    },
                    "suggested_resolutions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Suggested ways to resolve the blockage"
                    }
                },
                "required": ["blockage_type", "speech", "ask"]
            }
        }
    },
]

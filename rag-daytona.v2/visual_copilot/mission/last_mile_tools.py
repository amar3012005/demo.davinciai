"""
Tool Specifications for the TARA Compound Last-Mile Execution Agent.

These schemas define the specific rigid capabilities the Groq LLM can invoke
when attempting to autonomously resolve a goal in the `last_mile` phase.
"""

LAST_MILE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "click_element",
            "description": "Click a specific interactive element using its LiveGraph ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "The exact ID (e.g., 't-123') from the DOM list."
                    },
                    "why": {
                        "type": "string",
                        "description": "Reasoning for this click based on evidence."
                    }
                },
                "required": ["target_id", "why"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Input text into a field and optionally press enter.",
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
            "name": "request_vision",
            "description": "Trigger a multimodal visual scan using Set-of-Mark (SoM) when the text DOM is ambiguous, dynamic, or highly visual (e.g., charts, icons).",
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
            "name": "scroll_page",
            "description": "Scroll the view to reveal more interactable elements or readable content.",
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
            "description": "Wait briefly for asynchronous animations or loading spinners to finish.",
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
            "name": "complete_mission",
            "description": "Finalize the task. Use this when the goal is definitively achieved or when it is impossible to progress.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["success", "impossible"],
                        "description": "Whether you successfully achieved the goal or hit an impassable blockage."
                    },
                    "response": {
                        "type": "string",
                        "description": "The grounded textual answer or explanation for the user."
                    }
                },
                "required": ["status", "response"]
            }
        }
    }
]

from typing import Any, Dict


def fallback_response(reason: str) -> Dict[str, Any]:
    return {
        "success": False,
        "blocked": False,
        "complete": False,
        "action": None,
        "speech": "I hit a planning error and switched to fallback.",
        "pipeline": "vc.fallback",
        "reason": reason,
    }

"""
visual_copilot/api/domain_status.py

Modular owner for /api/v1/check_domain_status behavior.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger("vc.api.domain_status")


async def check_domain_status_modular(
    *,
    url: str,
    client_id: str,
    visual_orchestrator: Any,
) -> Dict[str, str]:
    """
    Check if current domain should run in mapped or explorer mode.
    """
    if visual_orchestrator is None:
        return {"mode": "explorer", "reason": "Visual Orchestrator not ready"}

    try:
        return await visual_orchestrator.check_hivemind_status(
            current_url=url,
            client_id=client_id,
        )
    except Exception as e:
        logger.error(f"Domain status check failed: {e}")
        return {"mode": "explorer", "reason": str(e)}


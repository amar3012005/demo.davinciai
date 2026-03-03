"""
visual_copilot/api/map_hints.py

Modular owner for /api/v1/get_map_hints behavior.
"""

import logging
from typing import Any, Dict, List, Optional

from visual_copilot.constants import ENABLE_PRE_ROUTER_VISION, PRE_ROUTER_VISION_MIN_CONF
from visual_copilot.orchestration.stages.page_relevance_gate import run_page_relevance_gate
from visual_copilot.orchestration.stages.pre_router_stage import run_pre_router_gate

logger = logging.getLogger("vc.api.map_hints")


async def build_map_hints(
    *,
    goal: str,
    client_id: str,
    current_url: str,
    screenshot_b64: Optional[str],
    mind_reader: Any,
    hive_interface: Any,
) -> Dict[str, str]:
    """
    Build map hints using modular Visual CoPilot components.
    """
    if not mind_reader or not hive_interface:
        logger.warning("⚠️ get_map_hints: modular services unavailable (mind_reader/hive_interface)")
        return {"hints": ""}

    logger.info(f"🗺️ Map Hints Request | Goal: '{goal}' | Client: {client_id}")
    if screenshot_b64:
        logger.info(f"🗺️ Map Hints screenshot_b64 present ({len(screenshot_b64) // 1024}KB)")
    else:
        logger.info("🗺️ Map Hints screenshot_b64 missing at API entry")

    try:
        # Optional pre-router vision decision BEFORE MindReader/Hive.
        if ENABLE_PRE_ROUTER_VISION:
            if screenshot_b64:
                try:
                    pre_router_response, pre_router_decision = await run_pre_router_gate(
                        goal=goal,
                        current_url=current_url or "",
                        screenshot_b64=screenshot_b64 or "",
                        session_id="map_hints",
                        logger=logger,
                    )
                    if pre_router_response:
                        action = pre_router_response.get("action", {}) or {}
                        target_domain = action.get("target_domain", "")
                        logger.info(
                            f"🗺️ Map Hints (vision pre-router) | cross_domain_shortcut=True "
                            f"target={target_domain or 'unknown'}"
                        )
                        return {"hints": f"CROSS_DOMAIN_TARGET: {target_domain}"}
                    if (
                        isinstance(pre_router_decision, dict)
                        and pre_router_decision.get("route") == "current_domain_last_mile"
                        and float(pre_router_decision.get("confidence") or 0.0) >= PRE_ROUTER_VISION_MIN_CONF
                    ):
                        conf = float(pre_router_decision.get("confidence") or 0.0)
                        logger.info(
                            f"🗺️ Map Hints (vision pre-router) | route=current_domain_last_mile conf={conf:.2f}"
                        )
                        return {"hints": "ROUTE_HINT: current_domain_last_mile"}
                except Exception as pre_err:
                    logger.warning(f"🗺️ Map Hints pre-router vision skipped due to error: {pre_err}")
            else:
                logger.info("🗺️ Map Hints pre-router vision skipped: screenshot_b64 missing")
        else:
            logger.info("🗺️ Map Hints pre-router vision disabled: ENABLE_PRE_ROUTER_VISION=false")

        schema = await mind_reader.translate(
            user_input=goal,
            current_url=current_url or ""
        )

        # Early page relevance gate: if goal clearly targets another domain,
        # skip Hive lookup and return immediate cross-domain guidance.
        gate_response, _ = run_page_relevance_gate(
            schema=schema,
            current_url=current_url or "",
            goal=goal,
            logger=logger,
        )
        if gate_response:
            action = gate_response.get("action", {})
            target_domain = action.get("target_domain", "")
            logger.info(
                f"🗺️ Map Hints (modular) | cross_domain_shortcut=True target={target_domain or 'unknown'}"
            )
            return {"hints": f"CROSS_DOMAIN_TARGET: {target_domain}"}

        # Domain-index guard: avoid costly multi-query Hive retrieval for unmapped sites.
        domain = getattr(schema, "domain", "") or ""
        if domain:
            indexed = await hive_interface.is_domain_indexed(domain)
            if not indexed:
                logger.info(f"🗺️ Map Hints (modular) | domain_not_indexed={domain} -> skip_hive")
                return {"hints": ""}

        hive_response = await hive_interface.retrieve(schema)

        hint_parts: List[str] = []
        if hive_response.strategy:
            hint_parts.append(
                "STRATEGY SEQUENCE: " + " -> ".join(hive_response.strategy.sequence)
            )
        if hive_response.visual_hints:
            rows = [
                f"- {h.text_pattern} ({h.element_type} in {h.zone}, selector={h.selector})"
                for h in hive_response.visual_hints[:8]
            ]
            hint_parts.append("VISUAL HINTS:\n" + "\n".join(rows))

        hints = "\n\n".join(hint_parts).strip()
        logger.info(
            f"🗺️ Map Hints (modular) | strategy={bool(hive_response.strategy)} hints={len(hive_response.visual_hints)}"
        )
        return {"hints": hints}
    except Exception as e:
        logger.warning(f"Map hints fetch failed: {e}")
        return {"hints": ""}

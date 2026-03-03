import re
from typing import Any, Dict, Optional, Tuple


def _root(d: str) -> str:
    parts = d.replace("https://", "").replace("http://", "").rstrip("/").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else d


async def run_cross_domain_gate(
    *,
    goal: str,
    action_history: Optional[list],
    effective_step: int,
    hive_response: Any,
    domain_known_for_hive: bool,
    schema: Any,
    session_id: str,
    current_url: str,
    app_state: Any,
    hive_interface: Any,
    logger,
) -> Tuple[Any, bool, Optional[Dict[str, Any]]]:
    is_zero_shot = False
    cache_hive = getattr(app_state, "_hive_cache", {})
    is_mid_mission = bool(action_history) or effective_step > 0

    if not hive_response.strategy:
        if not domain_known_for_hive:
            first_sg = getattr(schema, 'first_subgoal', None)
            if is_mid_mission:
                logger.info("⏩ Mid-mission with unknown domain strategy: keeping mission mode (no zero-shot reset).")
                return hive_response, False, None
            if first_sg:
                logger.info("🧭 ZERO-SHOT MODE: Hive bypassed (unknown domain), using Mind Reader first_subgoal.")
            else:
                logger.info("🧭 ZERO-SHOT MODE: Hive bypassed (unknown domain), no first_subgoal.")
            is_zero_shot = True
            return hive_response, is_zero_shot, None

        cross_domain_triggers = [r"\bgo to\b", r"\bopen\b", r"\bnavigate to\b", r"\btake me to\b", r"\bswitch to\b"]
        is_explicit_jump = any(re.search(t, goal.lower()) for t in cross_domain_triggers)

        cross_response = None
        # Only perform cross-domain retrieval when user intent explicitly asks to jump domains.
        if is_explicit_jump:
            cross_response = await hive_interface.retrieve_cross_domain(schema)
        else:
            logger.info("⏩ Cross-domain retrieval skipped: no explicit domain-jump intent.")

        bridge_domain = getattr(cross_response, "cross_domain_target", None) if cross_response else None
        garbage_domains = {"all", "none", "null", "", "any"}
        if bridge_domain and bridge_domain.lower().strip() in garbage_domains:
            bridge_domain = None

        is_co_domain_jump = False
        if bridge_domain:
            current_root = _root(schema.domain)
            target_root = _root(bridge_domain)
            is_co_domain_jump = current_root == target_root

        if (
            cross_response
            and (cross_response.strategy or cross_response.visual_hints)
            and bridge_domain
            and (is_explicit_jump or is_co_domain_jump)
        ):
            hive_response = cross_response
            cache_hive[session_id] = cross_response
            app_state._hive_cache = cache_hive
            logger.info(
                f"🧠 Cross-domain retrieval promoted: strategy={bool(cross_response.strategy)} "
                f"hints={len(cross_response.visual_hints)} target={bridge_domain}"
            )
        elif cross_response and (cross_response.strategy or cross_response.visual_hints):
            logger.info(
                f"CROSS_DOMAIN_REJECT reason=not_explicit_not_codomain target={bridge_domain} "
                f"explicit={is_explicit_jump} co_domain={is_co_domain_jump}"
            )

        if bridge_domain and (is_explicit_jump or is_co_domain_jump):
            current_host = schema.domain or ""
            if current_url and "://" in current_url:
                try:
                    from urllib.parse import urlparse
                    current_host = (urlparse(current_url).netloc or current_host).replace("www.", "")
                except Exception:
                    pass
            if current_host == bridge_domain:
                logger.info(
                    f"🌐 Cross-domain bridge target '{bridge_domain}' equals current host; continuing with semantic detective."
                )
            else:
                logger.info(f"🌐 Cross-Domain Bridge APPROVED: routing to '{bridge_domain}'")
                return hive_response, is_zero_shot, {
                    "success": True,
                    "blocked": False,
                    "action": {
                        "type": "cross_domain_navigate",
                        "target_domain": bridge_domain,
                        "target_entity": schema.target_entity,
                        "hints": [h.__dict__ for h in cross_response.visual_hints] if hasattr(cross_response, "visual_hints") else [],
                        "speech": f"I know where to find '{schema.target_entity}' on {bridge_domain}. Taking you there.",
                    },
                    "pipeline": "ultimate_tara_cross_domain",
                    "confidence": 0.90,
                }

        if is_mid_mission and not is_explicit_jump:
            logger.info("⏩ Mid-mission strategy miss: staying in mission mode (skip zero-shot).")
            is_zero_shot = False
        else:
            first_sg = getattr(schema, 'first_subgoal', None)
            if first_sg:
                logger.info("🧭 ZERO-SHOT MODE: Mind Reader provided first_subgoal → fast path (no ReAct needed)")
            else:
                logger.info("🧭 ZERO-SHOT MODE: No first_subgoal → will use ReAct LLM Router.")
            is_zero_shot = True

    return hive_response, is_zero_shot, None

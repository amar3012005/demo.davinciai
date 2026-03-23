from typing import Any, Optional

from tara_models import HiveResponse


async def resolve_hive_response(
    *,
    app_state: Any,
    hive_interface: Any,
    schema: Any,
    session_id: str,
    effective_step: int,
    skip_hive_prefetch: bool = False,
):
    domain_known_for_hive = True
    if skip_hive_prefetch:
        hive_response = HiveResponse(
            strategy=None,
            visual_hints=[],
            cached=False,
            query_time_ms=0,
        )
        cache_hive = getattr(app_state, "_hive_cache", {})
        if not hasattr(app_state, "_hive_cache"):
            app_state._hive_cache = cache_hive
        cache_hive[session_id] = hive_response
        return hive_response, False

    if effective_step == 0:
        domain_known_for_hive = await hive_interface.is_domain_indexed(getattr(schema, "domain", "") or "")
        if domain_known_for_hive:
            hive_response = await hive_interface.retrieve(schema)
        else:
            hive_response = HiveResponse(
                strategy=None,
                visual_hints=[],
                cached=False,
                query_time_ms=0,
            )
        cache_hive = getattr(app_state, "_hive_cache", {})
        if not hasattr(app_state, "_hive_cache"):
            app_state._hive_cache = cache_hive
        cache_hive[session_id] = hive_response
    else:
        cache_hive = getattr(app_state, "_hive_cache", {})
        hive_response = cache_hive.get(session_id)
        if not hive_response:
            domain_known_for_hive = await hive_interface.is_domain_indexed(getattr(schema, "domain", "") or "")
            if domain_known_for_hive:
                hive_response = await hive_interface.retrieve(schema)
            else:
                hive_response = HiveResponse(
                    strategy=None,
                    visual_hints=[],
                    cached=False,
                    query_time_ms=0,
                )
    return hive_response, domain_known_for_hive


def compute_location_guard_candidate(*, effective_step: int, action_history: Optional[list], logger) -> bool:
    if effective_step < 2:
        return False
    last_action_was_type = False
    if action_history:
        last_entry = action_history[-1]
        if isinstance(last_entry, str) and last_entry.startswith("type:"):
            last_action_was_type = True
    if last_action_was_type:
        logger.info("[SKIP] LOCATION GUARD Skipped: last action was a search (type). Waiting for results to render.")
        return False
    return True

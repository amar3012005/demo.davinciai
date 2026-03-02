from dataclasses import replace
from typing import Any, Optional, Tuple


def resolve_schema_cached(*, app_state: Any, session_id: str, goal: str, effective_step: int):
    cache = getattr(app_state, "_schema_cache", {})
    if not hasattr(app_state, "_schema_cache"):
        app_state._schema_cache = cache
    cached_entry = cache.get(session_id)
    if effective_step > 0 and cached_entry and cached_entry.get("goal") == goal:
        return cached_entry["schema"], True
    return None, False


async def parse_schema(
    *,
    mind_reader: Any,
    live_graph: Any,
    session_id: str,
    goal: str,
    current_url: str,
    previous_goal: Optional[str],
    logger,
):
    logger.info("👁️ Step 0: Live Graph getting DOM nodes for Mind Reader...")
    nodes = await live_graph.get_visible_nodes(session_id)
    logger.info("🧠 Step 1: Mind Reader parsing intent...")
    try:
        schema = await mind_reader.translate(
            user_input=goal,
            current_url=current_url,
            previous_goal=previous_goal,
            nodes=nodes,
        )
    except TypeError as e:
        # Compatibility path for deployments where MindReader.translate
        # has not yet adopted previous_goal/nodes kwargs.
        if "unexpected keyword argument" in str(e):
            logger.warning(
                f"MindReader.translate compatibility fallback: {e}. "
                "Retrying without optional kwargs."
            )
            schema = await mind_reader.translate(
                user_input=goal,
                current_url=current_url,
            )
        else:
            raise
    logger.info(f"   ✅ Intent: {schema.action.value} on '{schema.target_entity}'")
    return schema


def cache_schema(*, app_state: Any, session_id: str, goal: str, schema: Any) -> None:
    app_state._schema_cache[session_id] = {"schema": schema, "goal": goal}


def normalize_schema_domain(*, schema: Any, current_url: str, logger):
    if current_url and "://" in current_url:
        try:
            from urllib.parse import urlparse
            current_host = (urlparse(current_url).netloc or "").replace("www.", "")
        except Exception:
            current_host = ""
        if current_host and getattr(schema, "domain", "") != current_host:
            logger.info(
                f"DOMAIN_CONTEXT_OVERRIDE schema_domain={getattr(schema, 'domain', '') or 'none'} current_host={current_host}"
            )
            try:
                schema = replace(schema, domain=current_host)
            except Exception as exc:
                logger.warning(f"DOMAIN_CONTEXT_OVERRIDE failed to replace schema: {exc}")
    return schema


def compute_feature_flags(*, schema: Any, current_url: str, is_v3_feature_enabled, is_last_mile_enabled_for_domain, enable_keyword_direct_v3: bool, enable_subgoal_hint_query: bool, enable_verified_advance: bool):
    schema_domain = getattr(schema, "domain", "") or ""
    if not schema_domain and current_url and "://" in current_url:
        try:
            from urllib.parse import urlparse
            schema_domain = (urlparse(current_url).netloc or "").replace("www.", "")
        except Exception:
            schema_domain = ""

    keyword_direct_v3_active = is_v3_feature_enabled(schema_domain, enable_keyword_direct_v3)
    subgoal_hint_query_active = is_v3_feature_enabled(schema_domain, enable_subgoal_hint_query)
    verified_advance_active = is_v3_feature_enabled(schema_domain, enable_verified_advance)
    last_mile_enabled = is_last_mile_enabled_for_domain(schema_domain)
    return (
        schema_domain,
        keyword_direct_v3_active,
        subgoal_hint_query_active,
        verified_advance_active,
        last_mile_enabled,
    )

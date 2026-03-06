import pytest
from types import SimpleNamespace

from visual_copilot.orchestration.stages.router_pre_detective_stage import run_router_pre_detective_stage
from visual_copilot.routing.lexical_router import MatchResult


async def _noop_async(*args, **kwargs):
    return None


def _base_kwargs(query: str):
    mission = SimpleNamespace(
        mission_id="m1",
        action_history=[],
        current_subgoal_index=0,
        subgoals=["Click Docs"],
        strategy_locked=False,
        status="in_progress",
        phase="strategy",
    )
    schema = SimpleNamespace(target_entity="docs", zero_shot_mode=False)
    nodes = [SimpleNamespace(id="nav-docs", interactive=True, text="Docs", tag="a", zone="nav")]

    async def _record_and_maybe_advance(**kwargs):
        return 1

    async def _validate_and_end(*args, **kwargs):
        return {"success": True, "action": {"type": "answer"}, "speech": "done"}

    return {
        "app": SimpleNamespace(state=SimpleNamespace(mind_reader=SimpleNamespace(llm=None))),
        "session_id": "s1",
        "goal": "open docs",
        "current_url": "https://example.com",
        "start_time": 0.0,
        "mission": mission,
        "schema": schema,
        "nodes": nodes,
        "mission_brain": SimpleNamespace(
            _save_mission=_noop_async,
            advance_subgoal=_noop_async,
            _load_mission=_noop_async,
        ),
        "semantic_detective": SimpleNamespace(),
        "live_graph": SimpleNamespace(get_visible_nodes=_noop_async),
        "hive_response": SimpleNamespace(visual_hints=[]),
        "hive_interface": SimpleNamespace(retrieve_visual_hints_for_queries=_noop_async),
        "query": query,
        "domain_name": "example.com",
        "excluded_ids": set(),
        "is_zero_shot": False,
        "verified_advance_active": False,
        "current_dom_signature": "sig",
        "location_guard_candidate": "",
        "keyword_direct_v3_active": True,
        "subgoal_hint_query_active": False,
        "tara_router_v2_enabled": True,
        "tara_router_v2_shadow": True,
        "logger": SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None),
        "build_router_context_fn": lambda **k: {
            "subgoal_mode": "literal_click",
            "strategy_authoritative": False,
            "explicit_target_id_in_query": "nav-docs",
            "label_candidates": ["Docs"],
            "effective_hive_hints": [],
        },
        "classify_subgoal_mode_fn": lambda q: "literal_click",
        "override_mode_for_labels_fn": lambda mode, labels: mode,
        "extract_label_candidates_fn": lambda *a, **k: ["Docs"],
        "extract_explicit_target_id_fn": lambda q: "nav-docs",
        "validate_action_target_fn": lambda *a, **k: (True, "ok"),
        "record_and_maybe_advance_fn": _record_and_maybe_advance,
        "check_if_arrived_fn": _noop_async,
        "run_read_only_terminal_fn": _noop_async,
        "is_gallery_subgoal_fn": lambda q: False,
        "find_gallery_click_target_fn": lambda nodes, excluded: None,
        "build_label_policy_fn": lambda *a, **k: {"has_explicit_label": False, "label_candidates": []},
        "find_hard_keyword_match_fn": lambda **k: MatchResult(None, "", "none", None, "no_label_match"),
        "resolve_clickable_target_id_fn": lambda *a, **k: (None, "no"),
        "resolve_clickable_by_label_context_fn": lambda **k: (None, "no"),
        "extract_type_text_fn": lambda q, t: q,
        "retarget_click_to_nav_duplicate_if_needed_fn": lambda **k: (k["target_node"], "no"),
        "validate_and_end_mission_fn": _validate_and_end,
        "is_canary_domain_fn": lambda d: False,
        "match_result_cls": MatchResult,
    }


@pytest.mark.asyncio
async def test_pre_router_explicit_id_click_returns_action():
    result = await run_router_pre_detective_stage(**_base_kwargs("Click docs [ID: nav-docs]"))
    assert result["response"]["action"]["type"] == "click"
    assert result["response"]["action"]["target_id"] == "nav-docs"


@pytest.mark.asyncio
async def test_pre_router_ask_user_returns_clarify():
    kwargs = _base_kwargs("ask user: which docs section?")
    kwargs["build_router_context_fn"] = lambda **k: {
        "subgoal_mode": "ambiguous",
        "strategy_authoritative": False,
        "explicit_target_id_in_query": "",
        "label_candidates": [],
        "effective_hive_hints": [],
    }
    kwargs["extract_explicit_target_id_fn"] = lambda q: ""
    result = await run_router_pre_detective_stage(**kwargs)
    assert result["response"]["action"]["type"] == "clarify"


@pytest.mark.asyncio
async def test_pre_router_extract_and_present_uses_validator():
    kwargs = _base_kwargs("extract and present")
    kwargs["build_router_context_fn"] = lambda **k: {
        "subgoal_mode": "ambiguous",
        "strategy_authoritative": False,
        "explicit_target_id_in_query": "",
        "label_candidates": [],
        "effective_hive_hints": [],
    }
    kwargs["extract_explicit_target_id_fn"] = lambda q: ""
    result = await run_router_pre_detective_stage(**kwargs)
    assert result["response"]["success"] is True
    assert result["response"]["action"]["type"] == "answer"

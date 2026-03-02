from types import SimpleNamespace

import ultimate_api


def _node(node_id: str, *, tag: str = "a", role: str = "", zone: str = "main", text: str = "", interactive: bool = True):
    return SimpleNamespace(
        id=node_id,
        tag=tag,
        role=role,
        zone=zone,
        text=text,
        interactive=interactive,
    )


def test_validate_last_mile_step_rejects_unknown_id():
    nodes = [_node("n-1", tag="a", text="Dashboard")]
    ok, reason = ultimate_api._validate_last_mile_step(
        {"action": "click", "target_id": "missing"},
        nodes,
        set(),
    )
    assert ok is False
    assert reason == "target_not_in_live_graph"


def test_validate_last_mile_step_type_requires_input():
    nodes = [_node("n-1", tag="button", text="Usage")]
    ok, reason = ultimate_api._validate_last_mile_step(
        {"action": "type_text", "target_id": "n-1"},
        nodes,
        set(),
    )
    assert ok is False
    assert reason == "type_target_not_input"


def test_goal_completion_guard_detects_main_content_evidence():
    goal = "show model token usage in last 30 days"
    nodes = [
        _node("m-1", zone="main", text="Model token usage for last 30 days is available here.", interactive=False, tag="div"),
        _node("m-2", zone="main", text="Token usage by model in the last 30 days.", interactive=False, tag="div"),
        _node("n-1", zone="nav", text="Dashboard", interactive=True),
    ]
    assert ultimate_api._goal_completion_guard(goal, nodes) is True

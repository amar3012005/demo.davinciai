import types

import ultimate_api


def _node(
    node_id: str,
    text: str,
    tag: str = "a",
    zone: str = "nav",
    interactive: bool = True,
    state: str = "",
    value: str = "",
    aria_selected: str = "",
    aria_expanded: str = "",
):
    return types.SimpleNamespace(
        id=node_id,
        text=text,
        tag=tag,
        zone=zone,
        interactive=interactive,
        role="link" if tag == "a" else "",
        state=state,
        value=value,
        placeholder="",
        name="",
        aria_selected=aria_selected,
        aria_expanded=aria_expanded,
        parent_id=None,
    )


def test_extract_label_candidates_from_unquoted_navigation_phrase():
    schema = types.SimpleNamespace(first_subgoal=None)
    labels = ultimate_api._extract_label_candidates("Open Kaufen & Mieten", schema)
    assert any("kaufen" in l.lower() and "mieten" in l.lower() for l in labels)


def test_mode_override_for_labelled_navigation():
    assert (
        ultimate_api._override_mode_for_labels(
            "cognitive_navigate",
            ["Kaufen & Mieten"],
        )
        == "literal_click"
    )


def test_hard_keyword_match_uses_domain_synonyms():
    nodes = [
        _node("n1", "Buy & Rent", tag="a", zone="nav"),
    ]
    result = ultimate_api._find_hard_keyword_match(
        nodes=nodes,
        labels=["Kaufen & Mieten"],
        domain="engelvoelkers.com",
        subgoal_mode="literal_click",
        excluded_ids=set(),
    )
    assert result.candidate_id == "n1"
    assert result.reason == "ok"


def test_verify_pending_action_detects_target_state_change():
    mission = types.SimpleNamespace(
        pending_action={
            "type": "click",
            "target_id": "n1",
            "target_snapshot": {
                "state": "",
                "value": "",
                "aria_selected": "false",
                "aria_expanded": "false",
                "text": "Buy & Rent",
            },
        },
        last_url="https://www.engelvoelkers.com/de/de",
        last_dom_signature="same",
    )
    nodes = [_node("n1", "Buy & Rent", aria_selected="true", aria_expanded="false")]
    verified, reason = ultimate_api._verify_pending_action_effect(
        mission=mission,
        nodes=nodes,
        current_url="https://www.engelvoelkers.com/de/de",
        current_dom_signature="same",
    )
    assert verified is True
    assert reason == "target_state_changed"

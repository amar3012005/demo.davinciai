import asyncio
import time
import types

import pytest

from semantic_detective import SemanticDetective
from tara_models import GraphNode
import ultimate_api


class DummyLiveGraph:
    def __init__(self, nodes):
        self._nodes = nodes

    async def get_visible_nodes(self, session_id):
        return self._nodes


def _node(node_id: str, text: str, tag: str, zone: str, role: str = "") -> GraphNode:
    return GraphNode(
        id=node_id,
        tag=tag,
        text=text,
        role=role,
        zone=zone,
        interactive=True,
        visible=True,
        rect={"x": 0, "y": 0, "w": 100, "h": 24},
        parent_id=None,
        depth=1,
        state="",
        aria_selected=None,
        aria_expanded=None,
        timestamp=time.time(),
    )


def test_query_profile_extracts_focus_and_zone():
    detective = SemanticDetective(DummyLiveGraph([]), embeddings=None)
    profile = detective._build_query_profile("Click the Docs link in the top navigation")

    assert "docs" in profile.focus_terms
    assert profile.has_explicit_zone is True
    assert "nav" in profile.target_zones


def test_docs_nav_beats_visit_website_on_click_query():
    nodes = [
        _node("n1", "Visit Website", "a", "sidebar", role="link"),
        _node("n2", "Visit Website", "a", "nav", role="link"),
        _node("n3", "Docs", "a", "sidebar", role="link"),
        _node("n4", "Docs", "a", "nav", role="link"),
    ]
    detective = SemanticDetective(DummyLiveGraph(nodes), embeddings=None)

    sem_map = {
        "n1": 0.47,
        "n2": 0.47,
        "n3": 0.42,
        "n4": 0.42,
    }

    def fake_sem(self, node, query, query_vector=None, node_vector=None):
        return sem_map[node.id]

    detective._calculate_semantic_score = types.MethodType(fake_sem, detective)

    report = asyncio.run(detective.investigate(
        session_id="s1",
        query="Click the Docs link in the top navigation",
        hive_hints=[],
        subgoal_mode="literal_click",
    ))

    assert report.best_match is not None
    assert report.best_match.text == "Docs"
    assert report.best_match.zone == "nav"


def test_literal_type_prefers_input_over_button():
    nodes = [
        _node("search_input", "Search docs", "input", "nav", role="searchbox"),
        _node("btn_rate", "Rate limits", "button", "main", role="button"),
    ]
    detective = SemanticDetective(DummyLiveGraph(nodes), embeddings=None)

    sem_map = {
        "search_input": 0.55,
        "btn_rate": 0.62,
    }

    def fake_sem(self, node, query, query_vector=None, node_vector=None):
        return sem_map[node.id]

    detective._calculate_semantic_score = types.MethodType(fake_sem, detective)

    report = asyncio.run(detective.investigate(
        session_id="s2",
        query="Type 'rate limits' in search",
        hive_hints=[],
        subgoal_mode="literal_type",
    ))

    assert report.best_match is not None
    assert report.best_match.tag == "input"


def test_router_canary_domain_gate():
    original = ultimate_api.ROUTER_V2_CANARY_DOMAINS
    try:
        ultimate_api.ROUTER_V2_CANARY_DOMAINS = {"console.groq.com"}
        assert ultimate_api._is_canary_domain("console.groq.com") is True
        assert ultimate_api._is_canary_domain("example.com") is False
    finally:
        ultimate_api.ROUTER_V2_CANARY_DOMAINS = original


def test_incident_replay_docs_over_visit_website():
    nodes = [
        _node("visit_sidebar", "Visit Website", "a", "sidebar", role="link"),
        _node("visit_nav", "Visit Website", "a", "nav", role="link"),
        _node("docs_sidebar", "Docs", "a", "sidebar", role="link"),
        _node("docs_nav", "Docs", "a", "nav", role="link"),
    ]
    detective = SemanticDetective(DummyLiveGraph(nodes), embeddings=None)

    sem_map = {
        "visit_sidebar": 0.47,
        "visit_nav": 0.47,
        "docs_sidebar": 0.42,
        "docs_nav": 0.42,
    }

    def fake_sem(self, node, query, query_vector=None, node_vector=None):
        return sem_map[node.id]

    detective._calculate_semantic_score = types.MethodType(fake_sem, detective)

    report = asyncio.run(detective.investigate(
        session_id="incident",
        query="Click the Docs link in the top navigation",
        hive_hints=[],
        subgoal_mode="literal_click",
    ))

    assert report.best_match is not None
    assert report.best_match.text == "Docs"
    assert report.best_match.zone == "nav"
    assert report.confidence in {"medium", "high"}

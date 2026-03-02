"""
TARA v5 — Redis Page Graph (Semantic Page Graph)

Stores the full DOM semantically in Redis between steps.
Provides query-focused slicing: instead of sending the entire DOM to the LLM,
we score elements by relevance to the current goal and send only the top N.

This is Agent-E's "DOM distillation" + Mind2Web's "filter-then-reason"
combined into a single zero-LLM operation.

Usage:
    from redis_page_graph import PageGraphManager
    pgm = PageGraphManager(redis_client)
    graph = pgm.from_widget_data(elements, url, title, active_states, data_tables)
    await pgm.store(session_id, graph)
    slice_str = pgm.query_slice(graph, "find reasoning models", max_elements=60)
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SPG_PREFIX = "tara:spg:"
SPG_TTL = 600  # 10 minutes


@dataclass
class PageGraph:
    """Complete semantic representation of a web page, cached in Redis."""
    url: str
    title: str
    timestamp: float

    # Indexed structures
    zones: Dict[str, List[dict]] = field(default_factory=dict)
    nav_tree: List[dict] = field(default_factory=list)
    data_tables: List[dict] = field(default_factory=list)
    active_states: dict = field(default_factory=dict)

    # Raw elements (full DOM, not viewport-filtered)
    all_elements: List[dict] = field(default_factory=list)
    interactive_elements: List[dict] = field(default_factory=list)
    content_elements: List[dict] = field(default_factory=list)

    # Semantic index (word -> element IDs)
    concept_index: Dict[str, List[str]] = field(default_factory=dict)

    # History
    previous_url: Optional[str] = None
    step_number: int = 0


class PageGraphManager:
    """Manages the Semantic Page Graph in Redis."""

    def __init__(self, redis_client):
        self.redis = redis_client

    def from_widget_data(
        self,
        elements: List[dict],
        url: str,
        title: str = "",
        active_states: dict = None,
        data_tables: list = None,
        timestamp: float = 0.0,
    ) -> PageGraph:
        """Build a PageGraph from widget scan data."""
        import time as _time
        graph = PageGraph(
            url=url,
            title=title or "",
            timestamp=timestamp or _time.time(),
            all_elements=elements or [],
            active_states=active_states or {},
            data_tables=data_tables or [],
        )
        graph = self._rebuild_indices(graph)
        return graph

    async def store(self, session_id: str, graph: PageGraph):
        """Store full page graph in Redis with TTL."""
        if not self.redis:
            return
        key = f"{SPG_PREFIX}{session_id}"
        data = {
            "url": graph.url,
            "title": graph.title,
            "timestamp": graph.timestamp,
            "all_elements": graph.all_elements,
            "active_states": graph.active_states,
            "data_tables": graph.data_tables,
            "previous_url": graph.previous_url,
            "step_number": graph.step_number,
        }
        try:
            await self.redis.set(key, json.dumps(data, default=str), ex=SPG_TTL)
        except Exception as e:
            logger.warning(f"Failed to store PageGraph: {e}")

    async def load(self, session_id: str) -> Optional[PageGraph]:
        """Load cached page graph."""
        if not self.redis:
            return None
        key = f"{SPG_PREFIX}{session_id}"
        try:
            raw = await self.redis.get(key)
            if not raw:
                return None
            data = json.loads(raw)
            graph = PageGraph(
                url=data.get("url", ""),
                title=data.get("title", ""),
                timestamp=data.get("timestamp", 0.0),
                all_elements=data.get("all_elements", []),
                active_states=data.get("active_states", {}),
                data_tables=data.get("data_tables", []),
                previous_url=data.get("previous_url"),
                step_number=data.get("step_number", 0),
            )
            graph = self._rebuild_indices(graph)
            await self.redis.expire(key, SPG_TTL)
            return graph
        except Exception as e:
            logger.warning(f"Failed to load PageGraph: {e}")
            return None

    async def apply_diff(self, session_id: str, diff: dict) -> Optional[PageGraph]:
        """Apply incremental diff to cached graph."""
        graph = await self.load(session_id)
        if not graph:
            return None

        # Remove deleted elements
        removed_ids = set(diff.get("removed", []))
        if removed_ids:
            graph.all_elements = [e for e in graph.all_elements if e.get("id") not in removed_ids]

        # Add new elements
        added = diff.get("added", [])
        if added:
            existing_ids = {e.get("id") for e in graph.all_elements}
            for el in added:
                if el.get("id") not in existing_ids:
                    graph.all_elements.append(el)

        # Update changed elements
        changed = diff.get("changed", [])
        if changed:
            changed_map = {el.get("id"): el for el in changed if el.get("id")}
            for i, el in enumerate(graph.all_elements):
                if el.get("id") in changed_map:
                    graph.all_elements[i] = changed_map[el["id"]]

        # Update URL and active states
        if diff.get("url_changed"):
            graph.previous_url = graph.url
            graph.url = diff.get("new_url", graph.url)
        if "active_states" in diff:
            graph.active_states = diff["active_states"]
        if "data_tables" in diff:
            graph.data_tables = diff["data_tables"]

        # Rebuild indices
        graph = self._rebuild_indices(graph)
        graph.step_number += 1

        await self.store(session_id, graph)
        return graph

    def _rebuild_indices(self, graph: PageGraph) -> PageGraph:
        """Rebuild semantic indices from raw elements."""
        graph.zones = {}
        graph.interactive_elements = []
        graph.content_elements = []
        graph.concept_index = {}

        for el in graph.all_elements:
            zone = el.get("zone", "main")
            graph.zones.setdefault(zone, []).append(el)

            if el.get("interactive"):
                graph.interactive_elements.append(el)
            else:
                graph.content_elements.append(el)

            # Build concept index from text
            text = el.get("text", "").lower()
            for word in text.split():
                word = word.strip(".,;:!?()[]{}\"'")
                if len(word) > 3:
                    graph.concept_index.setdefault(word, []).append(el.get("id", ""))

        return graph

    def query_slice(self, graph: PageGraph, goal: str, max_elements: int = 60) -> str:
        """
        Query-focused DOM slicing (zero LLM cost, ~2ms).

        Instead of sending the full DOM or a viewport slice, we score elements
        by relevance to the current goal and send only the top N.
        """
        goal_lower = goal.lower()
        goal_words = set(w.strip(".,;:!?()[]{}\"'") for w in goal_lower.split() if len(w) > 2)

        scored_elements = []
        for el in graph.all_elements:
            score = 0
            text = el.get("text", "").lower()
            el_id = el.get("id", "")

            # Score 1: Direct text match with goal
            for word in goal_words:
                if word in text:
                    score += 10

            # Score 2: Interactive elements get base score
            if el.get("interactive"):
                score += 3

            # Score 3: Navigation elements always included
            if el.get("zone") == "nav":
                score += 5

            # Score 4: Active/selected state — only trust strict ARIA
            aria_current = el.get("ariaCurrent", "")
            if aria_current in ("page", "true", "step"):
                score += 8
            if el.get("ariaSelected") == "true":
                score += 6

            # Score 5: Headers/sections get bonus
            el_type = el.get("type", "div")
            if el_type in ("h1", "h2", "h3", "h4", "header", "nav"):
                score += 4

            # Score 6: Concept index match
            for word in goal_words:
                if el_id in graph.concept_index.get(word, []):
                    score += 7

            # Score 7: New elements get a boost
            if el.get("isNew") or el.get("is_new"):
                score += 3

            if score > 0:
                scored_elements.append((score, el))

        # Sort by score descending, take top N
        scored_elements.sort(key=lambda x: -x[0])
        selected = [el for _, el in scored_elements[:max_elements]]

        # Always include: active nav state + page URL + tables
        context = self._serialize_slice(graph, selected)
        return context

    def _serialize_slice(self, graph: PageGraph, elements: list) -> str:
        """Serialize a query-focused slice into the hierarchical format."""
        lines = []

        # Header: Page identity
        lines.append(f"URL: {graph.url}")
        active = graph.active_states or {}
        if active.get("activePage"):
            lines.append(f"ACTIVE PAGE: {active['activePage']}")
        if active.get("activeTab"):
            lines.append(f"ACTIVE TAB: {active['activeTab']}")

        # Tables: Serialize compactly
        if graph.data_tables:
            for i, table in enumerate(graph.data_tables[:2]):
                lines.append(f"\n## TABLE {i + 1}")
                if table.get("headers"):
                    lines.append(f"  | {' | '.join(str(h) for h in table['headers'])} |")
                for row in table.get("rows", [])[:10]:
                    lines.append(f"  | {' | '.join(str(c) for c in row)} |")

        # Group elements by zone
        by_zone = {}
        for el in elements:
            zone = el.get("zone", "main")
            by_zone.setdefault(zone, []).append(el)

        zone_order = ["nav", "main", "sidebar", "footer", "modal"]
        for zone in zone_order:
            if zone not in by_zone:
                continue
            lines.append(f"\n## {zone.upper()}")
            for el in by_zone[zone]:
                tag = el.get("type", "div")
                text = el.get("text", "")[:80]
                el_id = el.get("id", "")
                state = el.get("state", "")
                interactive = el.get("interactive", False)
                in_vp = el.get("inViewport", True)

                # State markers — only trust strict ARIA indicators
                markers = []
                aria_current = el.get("ariaCurrent", "")
                aria_selected = el.get("ariaSelected", "")
                if aria_current in ("page", "true", "step"):
                    markers.append("[active]")
                elif aria_selected == "true":
                    markers.append("[selected]")
                if el.get("ariaExpanded") == "true":
                    markers.append("[expanded]")
                if el.get("isNew") or el.get("is_new"):
                    markers.append("[NEW]")
                if not in_vp:
                    markers.append("[offscreen]")

                marker_str = " " + " ".join(markers) if markers else ""

                if interactive:
                    lines.append(f"  [{tag}] {text} (id: {el_id}){marker_str}")
                else:
                    lines.append(f"  ({tag}) {text}{marker_str}")

        return "\n".join(lines)

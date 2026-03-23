"""
live_graph.py

PURPOSE: Server-side DOM mirror backed by Redis. Maintains millisecond-accurate
         copy of browser's interactive elements for fast querying without
         touching the browser. Receives deltas from tara_sensor.js.

DEPENDENCIES:
    - redis.asyncio: Async Redis client for connection
    - json: For serialization
    - time: For timestamps
    - logging: For debug output
    - tara_models: GraphNode, DomDelta dataclasses

USED BY:
    - visual_orchestrator.py: Ingests dom_delta messages from WebSocket
    - semantic_detective.py: Queries get_visible_nodes() for candidates
    - mission_brain.py: May query graph for element verification

MIGRATION STATUS: [NEW] - Core perception layer for Ultimate TARA

Redis Key Schema:
    - Graph index: `graph:{session_id}` (Set of node IDs)
    - Node data: `graph:{session_id}:node:{node_id}` (JSON)
    - TTL: 3600s (1 hour)

Example:
    from live_graph import LiveGraph
    from redis import asyncio as aioredis
    
    redis = aioredis.from_url("redis://localhost:6379")
    live_graph = LiveGraph(redis)
    
    # Ingest delta from browser
    await live_graph.ingest_delta(session_id, delta_dict)
    
    # Query visible nodes
    nodes = await live_graph.get_visible_nodes(session_id)
    print(f"Found {len(nodes)} visible nodes")
"""

import json
import time
import logging
import os
from datetime import datetime
from typing import List, Optional, Dict, Set, Any
from redis import asyncio as aioredis

from tara_models import GraphNode, DomDelta

logger = logging.getLogger(__name__)

# Path where every full DOM scan is appended for inspection
# We prioritize an environment variable for Docker compatibility
_SCAN_LOG_PATH = os.environ.get("LIVE_GRAPH_SCAN_PATH")

if not _SCAN_LOG_PATH:
    # Local fallback
    _SCAN_LOG_PATH = os.path.join(
        os.path.dirname(__file__),          # rag-daytona.v2/
        "..",                               # demo.davinciai/
        "orchestra_daytona.v2",
        "live_graph_scan.md"
    )

_SCAN_LOG_PATH = os.path.normpath(_SCAN_LOG_PATH)


class LiveGraph:
    """
    Digital Twin of the browser DOM.
    Stores interactive elements in Redis for instant querying.
    
    Attributes:
        redis: Async Redis client
        TTL: Time-to-live for graph keys (default 3600s)
    
    Example:
        live_graph = LiveGraph(redis_client)
        await live_graph.ingest_delta(session_id, delta_data)
        nodes = await live_graph.get_visible_nodes(session_id)
    """

    def __init__(self, redis_client: aioredis.Redis, ttl: int = 3600):
        """
        Initialize LiveGraph with Redis client.
        
        Args:
            redis_client: Async Redis client instance
            ttl: Time-to-live for graph keys in seconds (default 3600)
        """
        self.redis = redis_client
        self.TTL = ttl
        # Tracking for session-based log rewriting
        self._last_logged_session = None
        logger.info(f"[GRAPH] LiveGraph initialized with TTL={ttl}s")

    def _graph_key(self, session_id: str) -> str:
        """
        Redis key for session graph index (Set of node IDs).
        
        Args:
            session_id: Session identifier
            
        Returns:
            Redis key string: "graph:{session_id}"
        """
        return f"graph:{session_id}"

    def _node_key(self, session_id: str, node_id: str) -> str:
        """
        Redis key for individual node data.
        
        Args:
            session_id: Session identifier
            node_id: Node identifier
            
        Returns:
            Redis key string: "graph:{session_id}:node:{node_id}"
        """
        return f"graph:{session_id}:node:{node_id}"

    # ═══════════════════════════════════════════════════════════
    # INGESTION (from tara_sensor.js)
    # ═══════════════════════════════════════════════════════════

    async def ingest_delta(self, session_id: str, delta: Dict[str, Any]) -> None:
        """
        Process incoming delta from browser sensor.
        Updates Redis graph with changes.
        
        Args:
            session_id: Session identifier
            delta: Delta dictionary with keys:
                - delta_type: "full_scan" | "update" | "add" | "remove"
                - nodes: List of GraphNode data (for add/update/full_scan)
                - removed_ids: List of node IDs to remove
                - url: Current page URL
                - timestamp: Delta timestamp
        
        Example:
            delta = {
                "delta_type": "update",
                "changes": [
                    {"type": "add", "node": {...}},
                    {"type": "remove", "id": "tara-123"}
                ],
                "url": "https://example.com",
                "timestamp": 1234567890.0
            }
            await live_graph.ingest_delta("session-456", delta)
        """
        delta_type = delta.get('delta_type', 'update')
        
        try:
            if delta_type == 'full_scan':
                await self._handle_full_scan(session_id, delta)
            elif delta_type in ('update', 'add', 'remove'):
                await self._handle_incremental_update(session_id, delta)
            else:
                logger.warning(f"Unknown delta_type: {delta_type}")
        except Exception as e:
            logger.error(f"Failed to ingest delta: {e}")
            raise

    async def _handle_full_scan(self, session_id: str, delta: Dict[str, Any]) -> None:
        """
        Replace entire graph with fresh scan.
        Clears old graph and stores all new nodes.
        
        Args:
            session_id: Session identifier
            delta: Delta with "nodes" list
        """
        nodes = delta.get('nodes', [])
        start_time = time.time()
        
        try:
            # Clear old graph
            old_keys = await self.redis.keys(f"graph:{session_id}:node:*")
            if old_keys:
                await self.redis.delete(*old_keys)
            
            # Store new nodes in pipeline
            pipe = self.redis.pipeline()
            node_ids = []
            
            for node_data in nodes:
                node_id = node_data.get('id', '')
                if not node_id:
                    continue
                    
                node_ids.append(node_id)
                node_key = self._node_key(session_id, node_id)
                
                # Store node as JSON with TTL
                pipe.set(
                    node_key,
                    json.dumps(node_data),
                    ex=self.TTL
                )
            
            # Update graph index (Set of node IDs)
            graph_key = self._graph_key(session_id)
            if node_ids:
                pipe.delete(graph_key)
                pipe.sadd(graph_key, *node_ids)
                pipe.expire(graph_key, self.TTL)
            
            await pipe.execute()
            
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.debug(
                f"Full scan: {len(nodes)} nodes stored for session {session_id} "
                f"({elapsed_ms}ms)"
            )

            # Persist scan to markdown for quality inspection (append, never overwrite)
            url = delta.get('url', 'unknown')
            await self._save_scan_to_markdown(session_id, nodes, url, elapsed_ms)
            
        except Exception as e:
            logger.error(f"Full scan failed: {e}")
            raise

    async def _save_scan_to_markdown(
        self,
        session_id: str,
        nodes: List[Dict[str, Any]],
        url: str,
        elapsed_ms: int
    ) -> None:
        """
        Append a human-readable DOM graph snapshot to live_graph_scan.md.

        Each scan gets its own timestamped section so the file grows
        chronologically and past scans remain for inspection.

        Format per entry:
        ---
        ## 📸 Scan — 2026-02-20 22:30:01
        **Session:** z9A0EOXIk  **URL:** https://console.groq.com/docs/…  **Nodes:** 251  ⏱ 10ms

        | # | ID | Tag | Zone | Interactive | Visible | Text |
        |---|----|----|------|-------------|---------|------|
        | 1 | tara-1 | a | nav | ✅ | ✅ | Playground |
        ...
        """
        try:
            # Detect if this is a new session
            is_new_session = (session_id != self._last_logged_session)
            mode = 'w' if is_new_session else 'a'
            
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            lines = []

            # If it's a new session, write a fresh header first
            if is_new_session:
                lines.extend([
                    f"# 📸 Live Graph Scan Log\n\n",
                    f"> Session started: `{ts}`\n",
                    f"> This file is rewritten every time TARA starts a NEW session.\n",
                    f"> Subsequent scans in the same session are appended below.\n\n",
                    f"---\n"
                ])
                self._last_logged_session = session_id

            lines.extend([
                f"\n---\n",
                f"## 📸 Scan — {ts}\n",
                f"**Session:** `{session_id[:12]}…`  "
                f"**URL:** `{url}`  "
                f"**Nodes:** {len(nodes)}  "
                f"⏱ {elapsed_ms}ms\n",
                f"\n",
                f"| # | Node ID | Tag | Zone | Interactive | Visible | Text |\n",
                f"|---|---------|-----|------|:-----------:|:-------:|------|\n",
            ])

            for i, node in enumerate(nodes, start=1):
                nid  = node.get('id', '')[:20]
                tag  = node.get('tag', node.get('type', '?'))[:10]
                zone = node.get('zone', '')[:12]
                inter = '✅' if node.get('interactive') else '—'
                vis   = '✅' if node.get('visible', True) else '—'
                # Truncate text to keep the table readable
                text  = str(node.get('text', '')).replace('|', '\\|').strip()[:60]
                lines.append(
                    f"| {i} | `{nid}` | {tag} | {zone} | {inter} | {vis} | {text} |\n"
                )

            # Ensure parent directory exists
            os.makedirs(os.path.dirname(_SCAN_LOG_PATH), exist_ok=True)

            # Write with the determined mode (w for new session, a for same session)
            with open(_SCAN_LOG_PATH, mode, encoding='utf-8') as f:
                f.writelines(lines)

            logger.debug(f"[NOTE] Scan appended to {_SCAN_LOG_PATH}")

        except Exception as e:
            # Never let file I/O crash the core pipeline
            logger.warning(f"[WARN] Could not write scan to markdown: {e}")

    async def _handle_incremental_update(self, session_id: str, delta: Dict[str, Any]) -> None:
        """
        Process incremental changes (add/update/remove).
        
        Args:
            session_id: Session identifier
            delta: Delta with "changes" list containing:
                - type: "add" | "update" | "remove"
                - node: GraphNode data (for add/update)
                - id: Node ID (for remove)
        """
        changes = delta.get('changes', [])
        start_time = time.time()
        
        try:
            pipe = self.redis.pipeline()
            graph_key = self._graph_key(session_id)
            
            added_count = 0
            updated_count = 0
            removed_count = 0
            
            for change in changes:
                change_type = change.get('type', '')
                
                if change_type == 'add':
                    node_data = change.get('node', {})
                    node_id = node_data.get('id', '')
                    if node_id:
                        node_key = self._node_key(session_id, node_id)
                        pipe.set(node_key, json.dumps(node_data), ex=self.TTL)
                        pipe.sadd(graph_key, node_id)
                        added_count += 1
                
                elif change_type == 'update':
                    node_data = change.get('node', {})
                    node_id = node_data.get('id', '')
                    if node_id:
                        node_key = self._node_key(session_id, node_id)
                        pipe.set(node_key, json.dumps(node_data), ex=self.TTL)
                        updated_count += 1
                
                elif change_type == 'remove':
                    node_id = change.get('id', '')
                    if node_id:
                        node_key = self._node_key(session_id, node_id)
                        pipe.delete(node_key)
                        pipe.srem(graph_key, node_id)
                        removed_count += 1
            
            # Refresh graph TTL
            pipe.expire(graph_key, self.TTL)
            
            await pipe.execute()
            
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.debug(
                f"Delta update: +{added_count} ~{updated_count} -{removed_count} "
                f"for session {session_id} ({elapsed_ms}ms)"
            )
            
        except Exception as e:
            logger.error(f"Incremental update failed: {e}")
            raise

    # ═══════════════════════════════════════════════════════════
    # QUERYING (for semantic_detective.py)
    # ═══════════════════════════════════════════════════════════

    async def get_all_nodes(self, session_id: str) -> List[GraphNode]:
        """
        Retrieve all nodes in graph.
        
        Args:
            session_id: Session identifier
            
        Returns:
            List of GraphNode objects
        """
        try:
            graph_key = self._graph_key(session_id)
            node_ids = await self.redis.smembers(graph_key)
            
            if not node_ids:
                return []
            
            # Fetch all nodes in pipeline
            pipe = self.redis.pipeline()
            for node_id in node_ids:
                # Handle bytes vs string
                if isinstance(node_id, bytes):
                    node_id = node_id.decode('utf-8')
                node_key = self._node_key(session_id, node_id)
                pipe.get(node_key)
            
            results = await pipe.execute()
            
            nodes = []
            for result in results:
                if result:
                    if isinstance(result, bytes):
                        result = result.decode('utf-8')
                    node_data = json.loads(result)
                    nodes.append(GraphNode.from_dict(node_data))
            
            return nodes
            
        except Exception as e:
            logger.error(f"get_all_nodes failed: {e}")
            return []

    async def get_visible_nodes(self, session_id: str) -> List[GraphNode]:
        """
        Get only visible nodes (filters out hidden elements).
        
        Args:
            session_id: Session identifier
            
        Returns:
            List of visible GraphNode objects
        """
        try:
            all_nodes = await self.get_all_nodes(session_id)
            visible = [n for n in all_nodes if n.visible]
            interactive = [n for n in visible if n.interactive]
            logger.debug(f"get_visible_nodes: {len(visible)} visible, {len(interactive)} interactive (of {len(all_nodes)} total)")
            return visible
        except Exception as e:
            logger.error(f"get_visible_nodes failed: {e}")
            return []

    async def get_interactive_nodes(self, session_id: str) -> List[GraphNode]:
        """
        Get only interactive elements (buttons, links, inputs).
        
        Args:
            session_id: Session identifier
            
        Returns:
            List of interactive GraphNode objects
        """
        try:
            all_nodes = await self.get_all_nodes(session_id)
            interactive = [n for n in all_nodes if n.interactive]
            logger.debug(f"get_interactive_nodes: {len(interactive)}/{len(all_nodes)} interactive")
            return interactive
        except Exception as e:
            logger.error(f"get_interactive_nodes failed: {e}")
            return []

    async def get_nodes_by_zone(self, session_id: str, zone: str) -> List[GraphNode]:
        """
        Filter nodes by zone (nav, main, modal, sidebar).
        
        Args:
            session_id: Session identifier
            zone: Zone name to filter by
            
        Returns:
            List of GraphNode objects in specified zone
        """
        try:
            all_nodes = await self.get_all_nodes(session_id)
            zone_nodes = [n for n in all_nodes if n.zone == zone]
            logger.debug(f"get_nodes_by_zone({zone}): {len(zone_nodes)} nodes")
            return zone_nodes
        except Exception as e:
            logger.error(f"get_nodes_by_zone failed: {e}")
            return []

    async def find_by_id(self, session_id: str, node_id: str) -> Optional[GraphNode]:
        """
        Get specific node by ID.
        
        Args:
            session_id: Session identifier
            node_id: Node identifier to find
            
        Returns:
            GraphNode if found, None otherwise
        """
        try:
            node_key = self._node_key(session_id, node_id)
            result = await self.redis.get(node_key)
            
            if result:
                if isinstance(result, bytes):
                    result = result.decode('utf-8')
                node_data = json.loads(result)
                return GraphNode.from_dict(node_data)
            
            logger.debug(f"find_by_id: {node_id} not found")
            return None
            
        except Exception as e:
            logger.error(f"find_by_id failed: {e}")
            return None

    async def find_by_text(
        self,
        session_id: str,
        text: str,
        fuzzy: bool = True
    ) -> List[GraphNode]:
        """
        Find nodes containing text.
        
        Args:
            session_id: Session identifier
            text: Text to search for
            fuzzy: If True, allows partial matches (default True)
            
        Returns:
            List of matching GraphNode objects
        """
        try:
            all_nodes = await self.get_all_nodes(session_id)
            text_lower = text.lower()
            
            if fuzzy:
                matches = [n for n in all_nodes if text_lower in n.text.lower()]
            else:
                matches = [n for n in all_nodes if text_lower == n.text.lower()]
            
            logger.debug(f"find_by_text('{text}'): {len(matches)} matches")
            return matches
            
        except Exception as e:
            logger.error(f"find_by_text failed: {e}")
            return []

    async def get_buttons(self, session_id: str) -> List[GraphNode]:
        """
        Get all button elements (tag=button or role=button).
        
        Args:
            session_id: Session identifier
            
        Returns:
            List of button GraphNode objects
        """
        try:
            all_nodes = await self.get_all_nodes(session_id)
            buttons = [
                n for n in all_nodes
                if n.tag == 'button' or (n.role and 'button' in n.role.lower())
            ]
            logger.debug(f"get_buttons: {len(buttons)} buttons")
            return buttons
        except Exception as e:
            logger.error(f"get_buttons failed: {e}")
            return []

    async def get_inputs(self, session_id: str) -> List[GraphNode]:
        """
        Get all input/textarea/select elements.
        
        Args:
            session_id: Session identifier
            
        Returns:
            List of input GraphNode objects
        """
        try:
            all_nodes = await self.get_all_nodes(session_id)
            inputs = [
                n for n in all_nodes
                if n.tag in ('input', 'textarea', 'select')
            ]
            logger.debug(f"get_inputs: {len(inputs)} inputs")
            return inputs
        except Exception as e:
            logger.error(f"get_inputs failed: {e}")
            return []

    async def get_links(self, session_id: str) -> List[GraphNode]:
        """
        Get all link elements (tag=a or role=link).
        
        Args:
            session_id: Session identifier
            
        Returns:
            List of link GraphNode objects
        """
        try:
            all_nodes = await self.get_all_nodes(session_id)
            links = [
                n for n in all_nodes
                if n.tag == 'a' or n.role == 'link'
            ]
            logger.debug(f"get_links: {len(links)} links")
            return links
        except Exception as e:
            logger.error(f"get_links failed: {e}")
            return []

    # ═══════════════════════════════════════════════════════════
    # GRAPH ANALYSIS
    # ═══════════════════════════════════════════════════════════

    async def get_stats(self, session_id: str) -> Dict[str, Any]:
        """
        Get graph statistics for debugging.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dictionary with stats:
                - total_nodes: Total node count
                - visible: Visible node count
                - interactive: Interactive node count
                - by_zone: Count grouped by zone
                - by_tag: Count grouped by tag
        """
        try:
            all_nodes = await self.get_all_nodes(session_id)
            
            by_zone: Dict[str, int] = {}
            by_tag: Dict[str, int] = {}
            
            for node in all_nodes:
                by_zone[node.zone] = by_zone.get(node.zone, 0) + 1
                by_tag[node.tag] = by_tag.get(node.tag, 0) + 1
            
            stats = {
                "total_nodes": len(all_nodes),
                "visible": len([n for n in all_nodes if n.visible]),
                "interactive": len([n for n in all_nodes if n.interactive]),
                "by_zone": by_zone,
                "by_tag": by_tag,
            }
            
            logger.debug(f"get_stats: {stats['total_nodes']} nodes")
            return stats
            
        except Exception as e:
            logger.error(f"get_stats failed: {e}")
            return {}

    async def clear_graph(self, session_id: str) -> None:
        """
        Remove all graph data for session.
        
        Args:
            session_id: Session identifier
        """
        try:
            keys = await self.redis.keys(f"graph:{session_id}*")
            if keys:
                await self.redis.delete(*keys)
                logger.info(f"[CLEANUP] Cleared graph for session {session_id}")
        except Exception as e:
            logger.error(f"clear_graph failed: {e}")

    async def node_exists(self, session_id: str, node_id: str) -> bool:
        """
        Check if a node exists in the graph.
        
        Args:
            session_id: Session identifier
            node_id: Node identifier
            
        Returns:
            True if node exists, False otherwise
        """
        try:
            node_key = self._node_key(session_id, node_id)
            result = await self.redis.exists(node_key)
            return result > 0
        except Exception as e:
            logger.error(f"node_exists failed: {e}")
            return False

    async def preflight_check(
        self,
        session_id: str,
        node_id: str
    ) -> Dict[str, Any]:
        """
        Pre-Flight Check — verify a target node is still live in Redis before
        the executor fires a click. Eliminates race-condition ElementHandle crashes.

        Validates:
        1. Node key exists in Redis (wasn't removed by a concurrent 'remove' delta)
        2. Node is still marked visible=True
        3. Node is still marked interactive=True

        Args:
            session_id: Session identifier
            node_id: Node to validate before clicking

        Returns:
            dict with keys:
                - valid (bool): True if safe to click
                - reason (str): Human-readable explanation if invalid
                - node (GraphNode | None): The live node data if valid
        """
        try:
            node = await self.find_by_id(session_id, node_id)

            if node is None:
                logger.warning(
                    f"PreFlight FAILED - node '{node_id}' no longer in Redis "
                    f"(likely removed by a concurrent DOM delta)"
                )
                return {"valid": False, "reason": "node_gone", "node": None}

            if not node.visible:
                logger.warning(f"[WARN] PreFlight FAILED - node '{node_id}' is hidden")
                return {"valid": False, "reason": "not_visible", "node": node}

            if not node.interactive:
                logger.warning(f"[WARN] PreFlight FAILED - node '{node_id}' not interactive")
                return {"valid": False, "reason": "not_interactive", "node": node}

            logger.debug(f"[OK] PreFlight PASSED - node '{node_id}' is live and clickable")
            return {"valid": True, "reason": "ok", "node": node}

        except Exception as e:
            logger.error(f"preflight_check failed: {e}")
            return {"valid": False, "reason": f"error: {e}", "node": None}

    async def update_node_state(
        self,
        session_id: str,
        node_id: str,
        state_updates: Dict[str, Any]
    ) -> bool:
        """
        Update specific fields of a node (e.g., state, aria_selected).
        
        Args:
            session_id: Session identifier
            node_id: Node identifier
            state_updates: Dictionary of fields to update
            
        Returns:
            True if successful, False otherwise
        """
        try:
            node = await self.find_by_id(session_id, node_id)
            if not node:
                logger.warning(f"update_node_state: node {node_id} not found")
                return False
            
            # Update fields
            for key, value in state_updates.items():
                if hasattr(node, key):
                    setattr(node, key, value)
            
            # Save back to Redis
            node_key = self._node_key(session_id, node_id)
            await self.redis.set(
                node_key,
                json.dumps(node.to_redis_dict()),
                ex=self.TTL
            )
            
            logger.debug(f"update_node_state: {node_id} updated")
            return True
            
        except Exception as e:
            logger.error(f"update_node_state failed: {e}")
            return False


# ═══════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ═══════════════════════════════════════════════════════════════

def create_live_graph(redis_url: str = "redis://localhost:6379") -> LiveGraph:
    """
    Factory function to create LiveGraph instance.
    
    Args:
        redis_url: Redis connection URL
        
    Returns:
        LiveGraph instance with connected Redis client
    """
    redis_client = aioredis.from_url(redis_url)
    return LiveGraph(redis_client)

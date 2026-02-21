TARA Ultimate Architecture: Complete Implementation Plan
Executive Architecture Overview
┌─────────────────────────────────────────────────────────────────┐
│                        USER INPUT                                │
│                     (Voice/Text/Command)                         │
└────────────────────────────┬────────────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │  mind_reader.py │ ◄── Translates to Schema
                    └────────┬────────┘
                             │ Tactical Schema
                    ┌────────▼─────────┐
                    │ hive_interface.py│ ◄── Retrieves Strategy + Hints
                    └────────┬─────────┘
                             │ Strategy Plan + Visual Hints
                    ┌────────▼──────────┐
                    │  mission_brain.py │ ◄── Creates Mission + Sub-goals
                    └────────┬──────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
    ┌─────────▼─────────┐        ┌─────────▼──────────┐
    │ BROWSER           │        │  REASONING LOOP    │
    │  tara_sensor.js   │        │  (mission_brain)   │
    └─────────┬─────────┘        └─────────┬──────────┘
              │ Deltas                      │
    ┌─────────▼─────────┐                  │
    │  live_graph.py    │◄─────────────────┤
    │  (Redis Mirror)   │   Queries Graph  │
    └─────────┬─────────┘                  │
              │                             │
    ┌─────────▼───────────┐                │
    │ semantic_detective.py│◄───────────────┘
    │ (Hybrid Scoring)     │   Requests Target
    └─────────┬────────────┘
              │ Target Node ID
    ┌─────────▼────────────┐
    │  mission_brain.py    │ ◄── Constraint Audit
    │  (Logic Guard)       │
    └─────────┬────────────┘
              │ Approved Action
    ┌─────────▼────────────┐
    │   EXECUTOR           │
    │  (Browser Widget)    │
    └──────────────────────┘

Phase 1: Data Structures & Schemas
1.1 Core Data Models (tara_models.py)
python"""
tara_models.py
Centralized data models for type safety across all modules.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Literal
from enum import Enum

# ═══════════════════════════════════════════════════════════════
# INPUT SCHEMA (From Mind Reader)
# ═══════════════════════════════════════════════════════════════

class ActionIntent(Enum):
    NAVIGATION = "navigation"      # Go somewhere
    EXTRACTION = "extraction"      # Read/find data
    INTERACTION = "interaction"    # Fill form, click button
    PURCHASE = "purchase"          # E-commerce flow
    SEARCH = "search"              # Search for items

@dataclass
class TacticalSchema:
    """
    Structured representation of user intent.
    Output of mind_reader.py
    """
    action: ActionIntent
    target_entity: str              # "T-shirt", "usage data", "settings"
    domain: str                     # "zalando.com", "groq.com"
    constraints: Dict[str, Optional[str]]  # {"color": "white", "size": None}
    raw_utterance: str              # Original user input
    timestamp: float
    
    def missing_constraints(self) -> List[str]:
        """Returns list of constraint keys that are None."""
        return [k for k, v in self.constraints.items() if v is None]
    
    def to_query_string(self) -> str:
        """Converts to Qdrant query: 'action:purchase domain:zalando'"""
        parts = [f"action:{self.action.value}", f"domain:{self.domain}"]
        for k, v in self.constraints.items():
            if v:
                parts.append(f"{k}:{v}")
        return " ".join(parts)

# ═══════════════════════════════════════════════════════════════
# HIVE MEMORY OUTPUTS
# ═══════════════════════════════════════════════════════════════

@dataclass
class StrategyHint:
    """
    High-level navigation sequence from Hive.
    For the Planner.
    """
    sequence: List[str]             # ["Search", "Select Item", "Choose Size", "Add to Bag"]
    constraints_order: List[str]    # ["color", "size", "quantity"]
    blocking_rules: Dict[str, List[str]]  # {"Add to Bag": ["size", "color"]}
    confidence: float
    source_url: str                 # Example URL this came from

@dataclass
class VisualHint:
    """
    Low-level element identifiers from Hive.
    For the Detective.
    """
    selector: str                   # "#picker-trigger", "button.add-to-cart"
    element_type: str               # "dropdown", "button", "input"
    text_pattern: Optional[str]     # "Choose size", "Add to.*"
    zone: str                       # "product_card", "nav", "sidebar"
    confidence: float

@dataclass
class HiveResponse:
    """Combined output from hive_interface.py"""
    strategy: Optional[StrategyHint]
    visual_hints: List[VisualHint]
    cached: bool
    query_time_ms: int

# ═══════════════════════════════════════════════════════════════
# LIVE GRAPH NODE (DOM Mirror)
# ═══════════════════════════════════════════════════════════════

@dataclass
class GraphNode:
    """
    Represents a single interactive element in the Live Graph.
    Stored in Redis as JSON.
    """
    id: str                         # "tara-abc123" (stable hash)
    tag: str                        # "button", "a", "input"
    text: str                       # Visible text content
    role: str                       # ARIA role
    zone: str                       # "nav", "main", "modal", "sidebar"
    interactive: bool
    visible: bool
    rect: Dict[str, float]          # {"x": 100, "y": 200, "w": 80, "h": 40}
    
    # Context (parent relationship)
    parent_id: Optional[str]
    depth: int
    
    # State
    state: str                      # "active", "disabled", "focused", ""
    aria_selected: Optional[str]
    aria_expanded: Optional[str]
    
    # Metadata
    timestamp: float                # When added to graph
    
    def to_redis_dict(self) -> dict:
        """Serializes to Redis-compatible dict."""
        return {
            "id": self.id,
            "tag": self.tag,
            "text": self.text,
            "role": self.role,
            "zone": self.zone,
            "interactive": self.interactive,
            "visible": self.visible,
            "rect": self.rect,
            "parent_id": self.parent_id or "",
            "depth": self.depth,
            "state": self.state,
            "timestamp": self.timestamp
        }

# ═══════════════════════════════════════════════════════════════
# MISSION STATE
# ═══════════════════════════════════════════════════════════════

class ConstraintStatus(Enum):
    MISSING = "MISSING"
    FILLED = "FILLED"
    INVALID = "INVALID"

@dataclass
class Constraint:
    name: str                       # "size", "color"
    value: Optional[str]
    status: ConstraintStatus
    
@dataclass
class MissionState:
    """
    Persistent mission tracking in Redis.
    Managed by mission_brain.py
    """
    mission_id: str
    session_id: str
    schema: TacticalSchema
    
    # Current state
    status: Literal["idle", "in_progress", "blocked", "completed", "failed"]
    current_subgoal_index: int
    subgoals: List[str]             # Generated from strategy
    
    # Constraints tracking
    constraints: Dict[str, Constraint]
    
    # History
    visited_urls: List[str]
    action_history: List[str]       # Compact action log
    ambiguity_count: int
    
    # Metadata
    created_at: float
    updated_at: float

# ═══════════════════════════════════════════════════════════════
# DETECTIVE OUTPUT
# ═══════════════════════════════════════════════════════════════

@dataclass
class ScoredCandidate:
    """A single candidate element with hybrid score."""
    node_id: str
    text: str
    tag: str
    zone: str
    
    # Scoring breakdown
    semantic_score: float           # 0.0 - 1.0 (vector similarity)
    hive_score: float               # 0.0 - 1.0 (hint match)
    hybrid_score: float             # Weighted combination
    
    # Evidence
    matched_hint: Optional[VisualHint]
    reasons: List[str]

@dataclass
class DetectiveReport:
    """Output of semantic_detective.py"""
    
    # Top candidates
    candidates: List[ScoredCandidate]
    best_match: Optional[ScoredCandidate]
    
    # Ambiguity detection
    is_ambiguous: bool              # True if multiple candidates within 0.1 score
    ambiguous_count: int
    
    # Obstacles
    has_obstacle: bool
    obstacle_type: Optional[str]    # "modal", "cookie_banner"
    dismiss_button_id: Optional[str]
    
    # Page analysis
    page_type: str                  # "form", "nav", "data", "search"
    
    # Routing
    recommended_action: str         # "click", "type", "scroll", "dismiss"
    confidence: Literal["high", "medium", "low"]

# ═══════════════════════════════════════════════════════════════
# DELTA MESSAGES (Browser → Server)
# ═══════════════════════════════════════════════════════════════

@dataclass
class DomDelta:
    """
    Incremental DOM update from tara_sensor.js
    """
    delta_type: Literal["add", "remove", "update", "full_scan"]
    nodes: List[GraphNode]
    removed_ids: List[str]
    url: str
    timestamp: float

Phase 2: Module Specifications
2.1 tara_sensor.js - The Vision Streamer
Location: Client-side (injected into browser)
Purpose: Replace snapshot-based DOM collection with delta streaming
Dependencies: None (vanilla JS)
Class Structure
javascript/**
 * tara_sensor.js
 * Real-time DOM change detector and streamer.
 * Replaces the bulk DOM snapshot logic in current tara-widget.js
 */

class TaraSensor {
    constructor(websocket, config = {}) {
        this.ws = websocket;
        this.config = {
            sendFullScanOnInit: true,
            debounceMs: 100,
            maxBatchSize: 50,
            ...config
        };
        
        this.observer = null;
        this.knownNodes = new Map();  // id -> NodeSnapshot
        this.pendingDeltas = [];
        this.debounceTimer = null;
    }

    /**
     * Initialize the MutationObserver.
     * Starts watching the DOM for changes.
     */
    start() {
        // Full scan on startup
        if (this.config.sendFullScanOnInit) {
            this.performFullScan();
        }

        // Setup MutationObserver
        this.observer = new MutationObserver((mutations) => {
            this.handleMutations(mutations);
        });

        this.observer.observe(document.body, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['class', 'aria-selected', 'aria-expanded', 'disabled']
        });
    }

    /**
     * Process mutation records.
     * Filters out noise, identifies deltas, queues for transmission.
     */
    handleMutations(mutations) {
        for (const mutation of mutations) {
            if (mutation.type === 'childList') {
                // Nodes added
                mutation.addedNodes.forEach(node => {
                    if (this.isInteractive(node)) {
                        this.registerNode(node, 'add');
                    }
                });

                // Nodes removed
                mutation.removedNodes.forEach(node => {
                    if (this.knownNodes.has(this.getNodeId(node))) {
                        this.pendingDeltas.push({
                            type: 'remove',
                            id: this.getNodeId(node)
                        });
                    }
                });
            } else if (mutation.type === 'attributes') {
                const node = mutation.target;
                if (this.isInteractive(node)) {
                    this.registerNode(node, 'update');
                }
            }
        }

        // Debounce transmission
        this.scheduleDeltaTransmission();
    }

    /**
     * Filter: Only interactive elements pass through.
     * Discards 95% of DOM (divs, spans, decorative elements).
     */
    isInteractive(node) {
        if (!(node instanceof Element)) return false;
        
        const tag = node.tagName.toLowerCase();
        
        // SVG noise - always reject
        const SVG_NOISE = new Set([
            'svg', 'path', 'rect', 'circle', 'line', 'polyline', 'polygon',
            'ellipse', 'use', 'defs', 'clippath', 'g', 'mask'
        ]);
        if (SVG_NOISE.has(tag)) return false;

        // Interactive tags
        const INTERACTIVE_TAGS = new Set([
            'button', 'a', 'input', 'select', 'textarea'
        ]);
        if (INTERACTIVE_TAGS.has(tag)) return true;

        // Role-based
        const role = node.getAttribute('role');
        if (role && ['button', 'link', 'menuitem', 'tab', 'option'].includes(role)) {
            return true;
        }

        // Clickable headers/labels with meaningful text
        if (['h1', 'h2', 'h3', 'label', 'th'].includes(tag)) {
            const text = this.extractText(node);
            return text.length > 2 && text.length < 100;
        }

        return false;
    }

    /**
     * Extract stable ID for node.
     * Uses existing data-tara-id or generates new one.
     */
    getNodeId(node) {
        let id = node.getAttribute('data-tara-id');
        if (!id) {
            id = this.generateStableId(node);
            node.setAttribute('data-tara-id', id);
        }
        return id;
    }

    /**
     * Generate stable hash-based ID.
     * Same algorithm as current widget.
     */
    generateStableId(node) {
        const tag = node.tagName.toLowerCase();
        const text = this.extractText(node).substring(0, 30);
        const role = node.getAttribute('role') || '';
        const href = node.getAttribute('href') || '';
        
        // DJB2 hash
        const key = `${tag}|${text}|${role}|${href}`;
        let hash = 5381;
        for (let i = 0; i < key.length; i++) {
            hash = ((hash << 5) + hash) ^ key.charCodeAt(i);
        }
        return `tara-${(hash >>> 0).toString(36)}`;
    }

    /**
     * Serialize node to GraphNode format.
     */
    serializeNode(node) {
        const rect = node.getBoundingClientRect();
        
        return {
            id: this.getNodeId(node),
            tag: node.tagName.toLowerCase(),
            text: this.extractText(node),
            role: node.getAttribute('role') || '',
            zone: this.classifyZone(node),
            interactive: true,
            visible: this.isVisible(node),
            rect: {
                x: Math.round(rect.left + window.scrollX),
                y: Math.round(rect.top + window.scrollY),
                w: Math.round(rect.width),
                h: Math.round(rect.height)
            },
            parent_id: this.getParentId(node),
            depth: this.getDepth(node),
            state: this.getState(node),
            aria_selected: node.getAttribute('aria-selected'),
            aria_expanded: node.getAttribute('aria-expanded'),
            timestamp: Date.now()
        };
    }

    /**
     * Queue delta for transmission.
     */
    registerNode(node, deltaType) {
        const serialized = this.serializeNode(node);
        this.knownNodes.set(serialized.id, serialized);
        
        this.pendingDeltas.push({
            type: deltaType,
            node: serialized
        });
    }

    /**
     * Debounced delta transmission.
     * Batches changes to avoid spamming the server.
     */
    scheduleDeltaTransmission() {
        if (this.debounceTimer) {
            clearTimeout(this.debounceTimer);
        }

        this.debounceTimer = setTimeout(() => {
            this.transmitDeltas();
        }, this.config.debounceMs);
    }

    /**
     * Send batched deltas to server.
     */
    transmitDeltas() {
        if (this.pendingDeltas.length === 0) return;

        const batch = this.pendingDeltas.splice(0, this.config.maxBatchSize);
        
        const message = {
            type: 'dom_delta',
            delta_type: 'update',
            changes: batch,
            url: window.location.href,
            timestamp: Date.now()
        };

        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(message));
        }
    }

    /**
     * Full DOM scan (startup only).
     */
    performFullScan() {
        const allInteractive = [];
        const walker = document.createTreeWalker(
            document.body,
            NodeFilter.SHOW_ELEMENT,
            null
        );

        let node;
        while (node = walker.nextNode()) {
            if (this.isInteractive(node)) {
                const serialized = this.serializeNode(node);
                this.knownNodes.set(serialized.id, serialized);
                allInteractive.push(serialized);
            }
        }

        const message = {
            type: 'dom_delta',
            delta_type: 'full_scan',
            nodes: allInteractive,
            url: window.location.href,
            timestamp: Date.now()
        };

        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(message));
        }
    }

    // ============ HELPER METHODS ============

    extractText(el) {
        const text = el.getAttribute('aria-label') || 
                     el.getAttribute('title') || 
                     el.getAttribute('placeholder') ||
                     el.textContent || 
                     el.value || 
                     '';
        return text.trim().substring(0, 80);
    }

    isVisible(el) {
        const style = window.getComputedStyle(el);
        return style.display !== 'none' && 
               style.visibility !== 'hidden' && 
               style.opacity !== '0';
    }

    classifyZone(el) {
        let node = el;
        let depth = 0;
        
        while (node && depth < 10) {
            const tag = node.tagName ? node.tagName.toLowerCase() : '';
            const role = node.getAttribute ? (node.getAttribute('role') || '') : '';
            
            if (tag === 'nav' || role === 'navigation') return 'nav';
            if (tag === 'aside') return 'sidebar';
            if (tag === 'footer') return 'footer';
            if (role === 'dialog') return 'modal';
            if (tag === 'main' || role === 'main') return 'main';
            
            node = node.parentElement;
            depth++;
        }
        
        return 'main';
    }

    getParentId(el) {
        let node = el.parentElement;
        while (node && node !== document.body) {
            const id = node.getAttribute('data-tara-id');
            if (id) return id;
            node = node.parentElement;
        }
        return null;
    }

    getDepth(el) {
        let depth = 0;
        let node = el;
        while (node && node !== document.documentElement) {
            node = node.parentElement;
            depth++;
        }
        return Math.min(depth, 20);
    }

    getState(el) {
        if (document.activeElement === el) return 'focused';
        if (el.getAttribute('aria-current')) return 'active';
        if (el.getAttribute('aria-selected') === 'true') return 'active';
        if (el.disabled) return 'disabled';
        return '';
    }

    stop() {
        if (this.observer) {
            this.observer.disconnect();
        }
        if (this.debounceTimer) {
            clearTimeout(this.debounceTimer);
        }
    }
}

// Export for integration
window.TaraSensor = TaraSensor;
Integration Points

Replace in tara-widget.js:

Remove scanPageBlueprint()
Remove captureDOMSnapshot()
Initialize TaraSensor on session start:



javascript// In startVisualCopilot():
this.sensor = new TaraSensor(this.ws, {
    sendFullScanOnInit: true,
    debounceMs: 150
});
this.sensor.start();

Message Handler:

Server receives {type: 'dom_delta', ...} instead of {type: 'dom_update', ...}




2.2 live_graph.py - The Redis Mirror
Purpose: Maintain millisecond-accurate DOM copy in Redis
Dependencies: redis, tara_models.py
Class Structure
python"""
live_graph.py
Server-side DOM mirror backed by Redis.
Provides fast querying without touching the browser.
"""

import json
import time
import logging
from typing import List, Optional, Dict, Set
from redis import Redis
from tara_models import GraphNode, DomDelta

logger = logging.getLogger(__name__)

class LiveGraph:
    """
    Digital Twin of the browser DOM.
    Stores interactive elements in Redis for instant querying.
    """
    
    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        self.TTL = 3600  # 1 hour session TTL
    
    def _graph_key(self, session_id: str) -> str:
        """Redis key for session graph."""
        return f"graph:{session_id}"
    
    def _node_key(self, session_id: str, node_id: str) -> str:
        """Redis key for individual node."""
        return f"graph:{session_id}:node:{node_id}"
    
    # ═══════════════════════════════════════════════════════════
    # INGESTION (from tara_sensor.js)
    # ═══════════════════════════════════════════════════════════
    
    def ingest_delta(self, session_id: str, delta: dict) -> None:
        """
        Process incoming delta from browser sensor.
        Updates Redis graph with changes.
        """
        delta_type = delta.get('delta_type')
        
        if delta_type == 'full_scan':
            self._handle_full_scan(session_id, delta)
        elif delta_type == 'update':
            self._handle_incremental_update(session_id, delta)
    
    def _handle_full_scan(self, session_id: str, delta: dict) -> None:
        """Replace entire graph with fresh scan."""
        nodes = delta.get('nodes', [])
        
        # Clear old graph
        old_keys = self.redis.keys(f"graph:{session_id}:node:*")
        if old_keys:
            self.redis.delete(*old_keys)
        
        # Store new nodes
        pipe = self.redis.pipeline()
        node_ids = []
        
        for node_data in nodes:
            node_id = node_data['id']
            node_ids.append(node_id)
            node_key = self._node_key(session_id, node_id)
            pipe.set(node_key, json.dumps(node_data), ex=self.TTL)
        
        # Update index
        graph_key = self._graph_key(session_id)
        pipe.delete(graph_key)
        if node_ids:
            pipe.sadd(graph_key, *node_ids)
            pipe.expire(graph_key, self.TTL)
        
        pipe.execute()
        logger.info(f"📸 Full scan: {len(nodes)} nodes stored for session {session_id}")
    
    def _handle_incremental_update(self, session_id: str, delta: dict) -> None:
        """Process incremental changes."""
        changes = delta.get('changes', [])
        
        pipe = self.redis.pipeline()
        graph_key = self._graph_key(session_id)
        
        for change in changes:
            change_type = change['type']
            
            if change_type == 'add':
                node_data = change['node']
                node_id = node_data['id']
                node_key = self._node_key(session_id, node_id)
                pipe.set(node_key, json.dumps(node_data), ex=self.TTL)
                pipe.sadd(graph_key, node_id)
            
            elif change_type == 'update':
                node_data = change['node']
                node_id = node_data['id']
                node_key = self._node_key(session_id, node_id)
                pipe.set(node_key, json.dumps(node_data), ex=self.TTL)
            
            elif change_type == 'remove':
                node_id = change['id']
                node_key = self._node_key(session_id, node_id)
                pipe.delete(node_key)
                pipe.srem(graph_key, node_id)
        
        pipe.expire(graph_key, self.TTL)
        pipe.execute()
    
    # ═══════════════════════════════════════════════════════════
    # QUERYING (for Detective)
    # ═══════════════════════════════════════════════════════════
    
    def get_all_nodes(self, session_id: str) -> List[GraphNode]:
        """Retrieve all nodes in graph."""
        graph_key = self._graph_key(session_id)
        node_ids = self.redis.smembers(graph_key)
        
        if not node_ids:
            return []
        
        nodes = []
        pipe = self.redis.pipeline()
        
        for node_id in node_ids:
            node_key = self._node_key(session_id, node_id.decode() if isinstance(node_id, bytes) else node_id)
            pipe.get(node_key)
        
        results = pipe.execute()
        
        for result in results:
            if result:
                node_data = json.loads(result)
                nodes.append(GraphNode(**node_data))
        
        return nodes
    
    def get_visible_nodes(self, session_id: str) -> List[GraphNode]:
        """Get only visible nodes."""
        all_nodes = self.get_all_nodes(session_id)
        return [n for n in all_nodes if n.visible]
    
    def get_nodes_by_zone(self, session_id: str, zone: str) -> List[GraphNode]:
        """Filter nodes by zone (nav, main, modal, etc)."""
        all_nodes = self.get_all_nodes(session_id)
        return [n for n in all_nodes if n.zone == zone]
    
    def get_interactive_nodes(self, session_id: str) -> List[GraphNode]:
        """Get only interactive elements."""
        all_nodes = self.get_all_nodes(session_id)
        return [n for n in all_nodes if n.interactive]
    
    def find_by_id(self, session_id: str, node_id: str) -> Optional[GraphNode]:
        """Get specific node by ID."""
        node_key = self._node_key(session_id, node_id)
        result = self.redis.get(node_key)
        
        if result:
            node_data = json.loads(result)
            return GraphNode(**node_data)
        return None
    
    def find_by_text(self, session_id: str, text: str, fuzzy: bool = True) -> List[GraphNode]:
        """
        Find nodes containing text.
        fuzzy=True allows partial matches.
        """
        all_nodes = self.get_all_nodes(session_id)
        text_lower = text.lower()
        
        if fuzzy:
            return [n for n in all_nodes if text_lower in n.text.lower()]
        else:
            return [n for n in all_nodes if text_lower == n.text.lower()]
    
    def get_buttons(self, session_id: str) -> List[GraphNode]:
        """Get all button elements."""
        all_nodes = self.get_all_nodes(session_id)
        return [n for n in all_nodes if n.tag == 'button' or 
                (n.role and 'button' in n.role)]
    
    def get_inputs(self, session_id: str) -> List[GraphNode]:
        """Get all input/textarea/select elements."""
        all_nodes = self.get_all_nodes(session_id)
        return [n for n in all_nodes if n.tag in ('input', 'textarea', 'select')]
    
    def get_links(self, session_id: str) -> List[GraphNode]:
        """Get all link elements."""
        all_nodes = self.get_all_nodes(session_id)
        return [n for n in all_nodes if n.tag == 'a' or n.role == 'link']
    
    # ═══════════════════════════════════════════════════════════
    # GRAPH ANALYSIS
    # ═══════════════════════════════════════════════════════════
    
    def get_stats(self, session_id: str) -> dict:
        """Get graph statistics."""
        all_nodes = self.get_all_nodes(session_id)
        
        return {
            "total_nodes": len(all_nodes),
            "visible": len([n for n in all_nodes if n.visible]),
            "interactive": len([n for n in all_nodes if n.interactive]),
            "by_zone": self._count_by_field(all_nodes, 'zone'),
            "by_tag": self._count_by_field(all_nodes, 'tag'),
        }
    
    def _count_by_field(self, nodes: List[GraphNode], field: str) -> dict:
        """Count nodes grouped by field value."""
        counts = {}
        for node in nodes:
            value = getattr(node, field, 'unknown')
            counts[value] = counts.get(value, 0) + 1
        return counts
    
    def clear_graph(self, session_id: str) -> None:
        """Remove all graph data for session."""
        keys = self.redis.keys(f"graph:{session_id}*")
        if keys:
            self.redis.delete(*keys)
Integration Points

In main WebSocket handler (main.py or equivalent):

python# Initialize
live_graph = LiveGraph(redis_client)

# On message
if msg_type == 'dom_delta':
    live_graph.ingest_delta(session_id, msg_data)

Usage in Detective:

python# Instead of receiving dom_elements from widget:
nodes = live_graph.get_visible_nodes(session_id)

2.3 mind_reader.py - The Input Translator
Purpose: Convert raw user input into structured TacticalSchema
Dependencies: groq_provider.py, tara_models.py
Class Structure
python"""
mind_reader.py
Translates messy human input into strict Tactical Schema.
First barrier between user chaos and system logic.
"""

import json
import logging
import re
from typing import Optional, Dict
from tara_models import TacticalSchema, ActionIntent

logger = logging.getLogger(__name__)

class MindReader:
    """
    Input sanitizer and schema generator.
    Uses fast LLM (Llama-3-8B) to structure user intent.
    """
    
    TRANSLATION_PROMPT = """You are an intent parser. Convert the user's request into a structured schema.

USER INPUT: "{user_input}"
CURRENT URL: {current_url}
CURRENT DOMAIN: {domain}

Extract:
1. ACTION: What type of task? (navigation, extraction, interaction, purchase, search)
2. TARGET ENTITY: What is the user asking about? (specific item, page, data)
3. CONSTRAINTS: Any filters or requirements mentioned? (color, size, date range, etc.)

CRITICAL RULES:
- If a constraint is mentioned but no value given, mark it as null
- If user says "white shirt" → constraints: {{"color": "white"}}
- If user says "shirt" → constraints: {{"color": null, "size": null}}
- Extract the domain from current_url if not explicitly mentioned

Output ONLY valid JSON:
{{
  "action": "purchase|navigation|extraction|interaction|search",
  "target_entity": "string",
  "domain": "example.com",
  "constraints": {{
    "key": "value or null"
  }}
}}

Examples:
User: "Show me white T-shirts"
Output: {{"action": "search", "target_entity": "T-shirt", "domain": "current", "constraints": {{"color": "white", "size": null}}}}

User: "Buy a medium shirt"
Output: {{"action": "purchase", "target_entity": "shirt", "domain": "current", "constraints": {{"size": "medium", "color": null}}}}

User: "Find my API usage"
Output: {{"action": "extraction", "target_entity": "API usage", "domain": "current", "constraints": {{}}}}
"""
    
    def __init__(self, llm_provider):
        """
        Args:
            llm_provider: Instance of GroqProvider or similar
        """
        self.llm = llm_provider
    
    async def translate(
        self, 
        user_input: str, 
        current_url: str = "",
        context: Optional[Dict] = None
    ) -> TacticalSchema:
        """
        Convert raw input to TacticalSchema.
        
        Args:
            user_input: Raw voice/text from user
            current_url: Current browser URL for context
            context: Optional additional context (conversation history, etc.)
        
        Returns:
            TacticalSchema with structured intent
        """
        # 1. Sanitize input
        cleaned = self._sanitize_input(user_input)
        
        # 2. Extract domain
        domain = self._extract_domain(current_url)
        
        # 3. Build prompt
        prompt = self.TRANSLATION_PROMPT.format(
            user_input=cleaned,
            current_url=current_url,
            domain=domain
        )
        
        # 4. Call LLM (fast model)
        try:
            response = await self.llm.generate(
                prompt,
                model="llama-3.1-8b-instant",
                temperature=0.1,
                max_tokens=300,
                response_format={"type": "json_object"}
            )
            
            # 5. Parse response
            data = json.loads(response)
            
            # 6. Validate and construct schema
            schema = TacticalSchema(
                action=ActionIntent(data.get('action', 'navigation')),
                target_entity=data.get('target_entity', ''),
                domain=data.get('domain', domain),
                constraints=data.get('constraints', {}),
                raw_utterance=user_input,
                timestamp=time.time()
            )
            
            logger.info(
                f"🧠 Mind Reader: '{user_input}' → "
                f"{schema.action.value} on '{schema.target_entity}' "
                f"(missing: {schema.missing_constraints()})"
            )
            
            return schema
            
        except Exception as e:
            logger.error(f"Mind Reader failed: {e}")
            # Fallback: simple schema
            return self._fallback_schema(user_input, domain)
    
    def _sanitize_input(self, text: str) -> str:
        """Remove filler words and clean input."""
        # Remove common filler
        fillers = ['um', 'uh', 'like', 'you know', 'basically', 'actually']
        cleaned = text
        for filler in fillers:
            cleaned = re.sub(rf'\b{filler}\b', '', cleaned, flags=re.IGNORECASE)
        
        # Normalize whitespace
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        
        return cleaned
    
    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL."""
        if not url:
            return "unknown"
        
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            domain = parsed.netloc.replace('www.', '')
            return domain
        except:
            return "unknown"
    
    def _fallback_schema(self, user_input: str, domain: str) -> TacticalSchema:
        """Simple fallback when LLM fails."""
        # Heuristic action detection
        input_lower = user_input.lower()
        
        if any(kw in input_lower for kw in ['buy', 'purchase', 'order', 'add to cart']):
            action = ActionIntent.PURCHASE
        elif any(kw in input_lower for kw in ['find', 'show', 'search', 'look for']):
            action = ActionIntent.SEARCH
        elif any(kw in input_lower for kw in ['click', 'select', 'choose']):
            action = ActionIntent.INTERACTION
        elif any(kw in input_lower for kw in ['data', 'usage', 'stats', 'metrics']):
            action = ActionIntent.EXTRACTION
        else:
            action = ActionIntent.NAVIGATION
        
        return TacticalSchema(
            action=action,
            target_entity=user_input[:50],
            domain=domain,
            constraints={},
            raw_utterance=user_input,
            timestamp=time.time()
        )
Integration Point
Replace the current _decompose_goal call in visual_orchestrator.py:
python# OLD:
# decomposed_plan = await self._decompose_goal(goal, dom_context, map_hints)

# NEW:
from mind_reader import MindReader

mind_reader = MindReader(self.groq)
schema = await mind_reader.translate(goal, current_url)

# Schema is now available for hive lookup and planning

2.4 hive_interface.py - Split-Brain Memory
Purpose: Retrieve both strategy (for Planner) and visual hints (for Detective) from Qdrant
Dependencies: qdrant_client, tara_models.py
Class Structure
python"""
hive_interface.py
Dual-retrieval system for Qdrant.
Returns BOTH high-level strategy AND low-level visual hints.
"""

import logging
import time
from typing import Optional, List
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from tara_models import TacticalSchema, StrategyHint, VisualHint, HiveResponse

logger = logging.getLogger(__name__)

class HiveInterface:
    """
    Split-brain memory retriever.
    Query A: Strategy sequences for planning.
    Query B: Element selectors for detection.
    """
    
    def __init__(self, qdrant_client: QdrantClient, embeddings, collection_name: str = "tara_hive"):
        self.qdrant = qdrant_client
        self.embeddings = embeddings
        self.collection = collection_name
    
    async def retrieve(self, schema: TacticalSchema) -> HiveResponse:
        """
        Dual retrieval: Strategy + Visual Hints.
        
        Args:
            schema: TacticalSchema from MindReader
        
        Returns:
            HiveResponse with both strategy and visual hints
        """
        start_time = time.time()
        
        # Parallel queries
        strategy_future = self._get_strategy(schema)
        hints_future = self._get_visual_hints(schema)
        
        strategy = await strategy_future
        hints = await hints_future
        
        query_time = int((time.time() - start_time) * 1000)
        
        logger.info(
            f"🧠 Hive: {schema.action.value} on {schema.domain} → "
            f"Strategy: {bool(strategy)}, Hints: {len(hints)} ({query_time}ms)"
        )
        
        return HiveResponse(
            strategy=strategy,
            visual_hints=hints,
            cached=False,  # TODO: implement cache
            query_time_ms=query_time
        )
    
    async def _get_strategy(self, schema: TacticalSchema) -> Optional[StrategyHint]:
        """
        Query A: High-level navigation strategy.
        Document type: "Strategy_Sequence"
        """
        query_text = f"strategy for {schema.action.value} {schema.target_entity} on {schema.domain}"
        
        try:
            results = await self.qdrant.query_points(
                collection_name=self.collection,
                query=self.embeddings.embed_query(query_text),
                query_filter=Filter(
                    must=[
                        FieldCondition(key="doc_type", match=MatchValue(value="Strategy_Sequence")),
                        FieldCondition(key="domain", match=MatchValue(value=schema.domain))
                    ]
                ),
                limit=1,
                score_threshold=0.7
            )
            
            if not results.points:
                logger.warning(f"No strategy found for {schema.domain}")
                return None
            
            point = results.points[0]
            payload = point.payload
            
            return StrategyHint(
                sequence=payload.get('sequence', []),
                constraints_order=payload.get('constraints_order', []),
                blocking_rules=payload.get('blocking_rules', {}),
                confidence=point.score,
                source_url=payload.get('example_url', '')
            )
            
        except Exception as e:
            logger.error(f"Strategy query failed: {e}")
            return None
    
    async def _get_visual_hints(self, schema: TacticalSchema) -> List[VisualHint]:
        """
        Query B: Low-level element selectors.
        Document type: "Visual_Hint"
        """
        # Query for entity-specific hints
        query_text = f"{schema.target_entity} selector on {schema.domain}"
        
        # Also query for constraint-specific hints if constraints exist
        constraint_queries = []
        for key, value in schema.constraints.items():
            if value:  # Only for filled constraints
                constraint_queries.append(f"{key} selector {value}")
        
        all_hints = []
        
        try:
            # Main entity query
            results = await self.qdrant.query_points(
                collection_name=self.collection,
                query=self.embeddings.embed_query(query_text),
                query_filter=Filter(
                    must=[
                        FieldCondition(key="doc_type", match=MatchValue(value="Visual_Hint")),
                        FieldCondition(key="domain", match=MatchValue(value=schema.domain))
                    ]
                ),
                limit=5,
                score_threshold=0.6
            )
            
            for point in results.points:
                payload = point.payload
                all_hints.append(VisualHint(
                    selector=payload.get('selector', ''),
                    element_type=payload.get('element_type', ''),
                    text_pattern=payload.get('text_pattern'),
                    zone=payload.get('zone', 'main'),
                    confidence=point.score
                ))
            
            # Constraint-specific queries
            for cq in constraint_queries:
                results = await self.qdrant.query_points(
                    collection_name=self.collection,
                    query=self.embeddings.embed_query(cq),
                    query_filter=Filter(
                        must=[
                            FieldCondition(key="doc_type", match=MatchValue(value="Visual_Hint")),
                            FieldCondition(key="domain", match=MatchValue(value=schema.domain))
                        ]
                    ),
                    limit=3,
                    score_threshold=0.6
                )
                
                for point in results.points:
                    payload = point.payload
                    all_hints.append(VisualHint(
                        selector=payload.get('selector', ''),
                        element_type=payload.get('element_type', ''),
                        text_pattern=payload.get('text_pattern'),
                        zone=payload.get('zone', 'main'),
                        confidence=point.score
                    ))
            
            # Deduplicate by selector
            seen = set()
            unique_hints = []
            for hint in all_hints:
                if hint.selector not in seen:
                    seen.add(hint.selector)
                    unique_hints.append(hint)
            
            return unique_hints
            
        except Exception as e:
            logger.error(f"Visual hints query failed: {e}")
            return []
    
    async def store_strategy(
        self,
        domain: str,
        action: str,
        sequence: List[str],
        constraints_order: List[str],
        blocking_rules: Dict[str, List[str]],
        example_url: str
    ) -> bool:
        """
        Store a new strategy sequence in the Hive.
        Called by Explorer mode after successful mission completion.
        """
        from qdrant_client.models import PointStruct
        import uuid
        
        payload = {
            "doc_type": "Strategy_Sequence",
            "domain": domain,
            "action": action,
            "sequence": sequence,
            "constraints_order": constraints_order,
            "blocking_rules": blocking_rules,
            "example_url": example_url,
            "text": f"Strategy for {action} on {domain}: {' -> '.join(sequence)}"
        }
        
        try:
            embedding = self.embeddings.embed_query(payload["text"])
            
            point = PointStruct(
                id=str(uuid.uuid4()),
                vector=embedding,
                payload=payload
            )
            
            await self.qdrant.upsert(
                collection_name=self.collection,
                points=[point]
            )
            
            logger.info(f"📝 Stored strategy for {action} on {domain}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to store strategy: {e}")
            return False
    
    async def store_visual_hint(
        self,
        domain: str,
        selector: str,
        element_type: str,
        text_pattern: Optional[str],
        zone: str,
        description: str
    ) -> bool:
        """
        Store a new visual hint in the Hive.
        """
        from qdrant_client.models import PointStruct
        import uuid
        
        payload = {
            "doc_type": "Visual_Hint",
            "domain": domain,
            "selector": selector,
            "element_type": element_type,
            "text_pattern": text_pattern,
            "zone": zone,
            "text": description
        }
        
        try:
            embedding = self.embeddings.embed_query(description)
            
            point = PointStruct(
                id=str(uuid.uuid4()),
                vector=embedding,
                payload=payload
            )
            
            await self.qdrant.upsert(
                collection_name=self.collection,
                points=[point]
            )
            
            logger.info(f"📝 Stored visual hint: {selector} for {domain}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to store visual hint: {e}")
            return False
Integration Point
After MindReader:
python# Generate schema
schema = await mind_reader.translate(goal, current_url)

# Query Hive
hive = HiveInterface(qdrant_client, embeddings)
hive_response = await hive.retrieve(schema)

# Use strategy for planning
if hive_response.strategy:
    session.subgoals = hive_response.strategy.sequence
    session.constraint_rules = hive_response.strategy.blocking_rules

# Visual hints passed to Detective

2.5 semantic_detective.py - Hybrid Scoring Engine
Purpose: Find target elements using semantic similarity + Hive hints
Dependencies: live_graph.py, tara_models.py, sentence transformers
Class Structure
python"""
semantic_detective.py
Hybrid element finder: Meaning + Memory.
Replaces current detective.py with vector-based scoring.
"""

import logging
import re
from typing import List, Optional, Tuple
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

from live_graph import LiveGraph
from tara_models import (
    GraphNode, VisualHint, ScoredCandidate, DetectiveReport, TacticalSchema
)

logger = logging.getLogger(__name__)

class SemanticDetective:
    """
    Target acquisition via hybrid scoring.
    Formula: Score = (0.6 * Semantic) + (0.4 * Hive Hint Match)
    """
    
    # Scoring weights
    SEMANTIC_WEIGHT = 0.6
    HIVE_WEIGHT = 0.4
    
    # Ambiguity threshold
    AMBIGUITY_THRESHOLD = 0.1  # Candidates within 0.1 score = ambiguous
    
    def __init__(self, live_graph: LiveGraph, model_name: str = "all-MiniLM-L6-v2"):
        self.graph = live_graph
        self.model = SentenceTransformer(model_name)
        
    async def investigate(
        self,
        session_id: str,
        schema: TacticalSchema,
        visual_hints: List[VisualHint],
        subgoal: Optional[str] = None
    ) -> DetectiveReport:
        """
        Find the best target element for current goal.
        
        Args:
            session_id: Current session
            schema: User intent from MindReader
            visual_hints: Element hints from Hive
            subgoal: Current sub-goal text (optional refinement)
        
        Returns:
            DetectiveReport with scored candidates
        """
        # 1. Get nodes from Live Graph
        nodes = self.graph.get_visible_nodes(session_id)
        
        if not nodes:
            logger.warning("No visible nodes in graph")
            return self._empty_report()
        
        # 2. Check for obstacles
        obstacle_info = self._detect_obstacles(nodes)
        
        # 3. Classify page
        page_type = self._classify_page(nodes)
        
        # 4. Build search query (goal + subgoal)
        search_text = schema.target_entity
        if subgoal:
            search_text = f"{search_text} {subgoal}"
        
        # 5. Score candidates
        scored = self._score_candidates(
            nodes=nodes,
            search_text=search_text,
            visual_hints=visual_hints
        )
        
        # 6. Detect ambiguity
        is_ambiguous, ambiguous_count = self._check_ambiguity(scored)
        
        # 7. Recommend action
        action, confidence = self._recommend_action(
            scored=scored,
            page_type=page_type,
            has_obstacle=obstacle_info[0],
            is_ambiguous=is_ambiguous
        )
        
        return DetectiveReport(
            candidates=scored[:10],
            best_match=scored[0] if scored else None,
            is_ambiguous=is_ambiguous,
            ambiguous_count=ambiguous_count,
            has_obstacle=obstacle_info[0],
            obstacle_type=obstacle_info[1],
            dismiss_button_id=obstacle_info[2],
            page_type=page_type,
            recommended_action=action,
            confidence=confidence
        )
    
    def _score_candidates(
        self,
        nodes: List[GraphNode],
        search_text: str,
        visual_hints: List[VisualHint]
    ) -> List[ScoredCandidate]:
        """
        Hybrid scoring: Semantic + Hive hints.
        """
        if not nodes:
            return []
        
        # Embed search query
        query_embedding = self.model.encode([search_text])[0]
        
        # Embed all node texts
        node_texts = [n.text if n.text else f"{n.tag} {n.role}" for n in nodes]
        node_embeddings = self.model.encode(node_texts)
        
        # Compute semantic similarities
        semantic_scores = cosine_similarity(
            [query_embedding],
            node_embeddings
        )[0]
        
        # Score each node
        candidates = []
        
        for i, node in enumerate(nodes):
            # A: Semantic score (0-1)
            semantic = float(semantic_scores[i])
            
            # B: Hive hint score (0-1)
            hive_score, matched_hint = self._match_hive_hints(node, visual_hints)
            
            # Hybrid score
            hybrid = (self.SEMANTIC_WEIGHT * semantic) + (self.HIVE_WEIGHT * hive_score)
            
            # Build candidate
            reasons = []
            if semantic > 0.5:
                reasons.append(f"semantic={semantic:.2f}")
            if hive_score > 0:
                reasons.append(f"hint_match={hive_score:.2f}")
            
            candidates.append(ScoredCandidate(
                node_id=node.id,
                text=node.text[:60],
                tag=node.tag,
                zone=node.zone,
                semantic_score=semantic,
                hive_score=hive_score,
                hybrid_score=hybrid,
                matched_hint=matched_hint,
                reasons=reasons
            ))
        
        # Sort by hybrid score
        candidates.sort(key=lambda c: c.hybrid_score, reverse=True)
        
        return candidates
    
    def _match_hive_hints(
        self,
        node: GraphNode,
        hints: List[VisualHint]
    ) -> Tuple[float, Optional[VisualHint]]:
        """
        Check if node matches any Hive visual hint.
        Returns (score, matched_hint).
        """
        if not hints:
            return (0.0, None)
        
        best_score = 0.0
        best_hint = None
        
        for hint in hints:
            score = 0.0
            
            # Match 1: Exact ID match (strongest signal)
            if hint.selector.startswith('#'):
                hint_id = hint.selector[1:]
                if hint_id in node.id:
                    score = 1.0
            
            # Match 2: Class match
            elif hint.selector.startswith('.'):
                # Node doesn't have class info in current model
                # TODO: add class field to GraphNode
                pass
            
            # Match 3: Tag + role match
            elif hint.element_type:
                if hint.element_type == node.tag or hint.element_type in node.role:
                    score += 0.5
            
            # Match 4: Text pattern
            if hint.text_pattern:
                pattern = re.compile(hint.text_pattern, re.IGNORECASE)
                if pattern.search(node.text):
                    score += 0.5
            
            # Match 5: Zone match
            if hint.zone == node.zone:
                score += 0.2
            
            # Weight by hint confidence
            score *= hint.confidence
            
            if score > best_score:
                best_score = score
                best_hint = hint
        
        return (best_score, best_hint)
    
    def _detect_obstacles(self, nodes: List[GraphNode]) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Detect modals, cookie banners.
        Returns (has_obstacle, type, dismiss_button_id).
        """
        # Check for modal zone
        modal_nodes = [n for n in nodes if n.zone == 'modal']
        
        if modal_nodes:
            # Look for dismiss button
            dismiss_keywords = {'close', 'dismiss', 'x', 'accept', 'ok', 'got it'}
            for node in modal_nodes:
                text_lower = node.text.lower()
                if any(kw in text_lower for kw in dismiss_keywords):
                    return (True, 'modal', node.id)
            
            return (True, 'modal', None)
        
        # Check for cookie banner text
        cookie_keywords = {'cookie', 'consent', 'privacy', 'gdpr'}
        for node in nodes:
            text_lower = node.text.lower()
            if any(kw in text_lower for kw in cookie_keywords):
                # Look for accept button nearby
                for candidate in nodes:
                    if 'accept' in candidate.text.lower() or 'agree' in candidate.text.lower():
                        return (True, 'cookie_banner', candidate.id)
                
                return (True, 'cookie_banner', None)
        
        return (False, None, None)
    
    def _classify_page(self, nodes: List[GraphNode]) -> str:
        """Classify page type."""
        input_count = len([n for n in nodes if n.tag in ('input', 'textarea', 'select')])
        
        if input_count >= 3:
            return 'form'
        elif input_count == 1:
            return 'search'
        else:
            return 'nav'
    
    def _check_ambiguity(self, candidates: List[ScoredCandidate]) -> Tuple[bool, int]:
        """
        Check if top candidates are too close in score.
        Returns (is_ambiguous, count_of_ambiguous).
        """
        if len(candidates) < 2:
            return (False, 0)
        
        top_score = candidates[0].hybrid_score
        ambiguous = [c for c in candidates if abs(c.hybrid_score - top_score) <= self.AMBIGUITY_THRESHOLD]
        
        is_ambiguous = len(ambiguous) > 1
        return (is_ambiguous, len(ambiguous) if is_ambiguous else 0)
    
    def _recommend_action(
        self,
        scored: List[ScoredCandidate],
        page_type: str,
        has_obstacle: bool,
        is_ambiguous: bool
    ) -> Tuple[str, str]:
        """
        Recommend action and confidence.
        Returns (action, confidence).
        """
        if has_obstacle:
            return ("dismiss", "high")
        
        if is_ambiguous:
            return ("clarify", "low")
        
        if not scored:
            return ("scroll", "low")
        
        top = scored[0]
        
        if top.hybrid_score > 0.7:
            confidence = "high"
        elif top.hybrid_score > 0.4:
            confidence = "medium"
        else:
            confidence = "low"
        
        if page_type == 'form':
            return ("type", confidence)
        else:
            return ("click", confidence)
    
    def _empty_report(self) -> DetectiveReport:
        """Fallback empty report."""
        return DetectiveReport(
            candidates=[],
            best_match=None,
            is_ambiguous=False,
            ambiguous_count=0,
            has_obstacle=False,
            obstacle_type=None,
            dismiss_button_id=None,
            page_type='unknown',
            recommended_action='wait',
            confidence='low'
        )
Integration Point
Replace current investigate() call:
python# OLD:
# from detective import investigate
# report = investigate(goal, subgoal, dom_elements, ...)

# NEW:
from semantic_detective import SemanticDetective

detective = SemanticDetective(live_graph)
report = await detective.investigate(
    session_id=session_id,
    schema=schema,
    visual_hints=hive_response.visual_hints,
    subgoal=current_subgoal_desc
)

2.6 mission_brain.py - The Logic Guard
Purpose: Maintain mission state and enforce constraint rules
Dependencies: redis, tara_models.py
Class Structure
python"""
mission_brain.py
The reasoning engine and state manager.
Blocks actions that violate constraint rules.
"""

import json
import logging
import time
from typing import Optional, List, Dict, Tuple
from redis import Redis
from tara_models import (
    MissionState, TacticalSchema, Constraint, ConstraintStatus,
    StrategyHint, DetectiveReport
)

logger = logging.getLogger(__name__)

class MissionBrain:
    """
    Mission state manager + constraint enforcer.
    The "adult in the room" that prevents premature actions.
    """
    
    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        self.TTL = 3600
    
    def _mission_key(self, session_id: str) -> str:
        return f"mission:{session_id}"
    
    # ═══════════════════════════════════════════════════════════
    # MISSION CREATION
    # ═══════════════════════════════════════════════════════════
    
    def create_mission(
        self,
        session_id: str,
        schema: TacticalSchema,
        strategy: Optional[StrategyHint]
    ) -> MissionState:
        """
        Initialize new mission from schema and strategy.
        """
        # Build constraints dict
        constraints = {}
        for key, value in schema.constraints.items():
            status = ConstraintStatus.FILLED if value else ConstraintStatus.MISSING
            constraints[key] = Constraint(name=key, value=value, status=status)
        
        # Build sub-goals from strategy
        subgoals = strategy.sequence if strategy else [schema.target_entity]
        
        mission = MissionState(
            mission_id=f"{session_id}_{int(time.time())}",
            session_id=session_id,
            schema=schema,
            status="in_progress",
            current_subgoal_index=0,
            subgoals=subgoals,
            constraints=constraints,
            visited_urls=[],
            action_history=[],
            ambiguity_count=0,
            created_at=time.time(),
            updated_at=time.time()
        )
        
        self._save(mission)
        
        logger.info(
            f"🎯 Mission created: {schema.action.value} '{schema.target_entity}' "
            f"with {len(subgoals)} steps"
        )
        
        return mission
    
    # ═══════════════════════════════════════════════════════════
    # STATE MANAGEMENT
    # ═══════════════════════════════════════════════════════════
    
    def get_mission(self, session_id: str) -> Optional[MissionState]:
        """Load mission from Redis."""
        key = self._mission_key(session_id)
        data = self.redis.get(key)
        
        if not data:
            return None
        
        return self._deserialize(json.loads(data))
    
    def _save(self, mission: MissionState) -> None:
        """Persist mission to Redis."""
        mission.updated_at = time.time()
        key = self._mission_key(mission.session_id)
        self.redis.set(key, json.dumps(self._serialize(mission)), ex=self.TTL)
    
    def update_constraint(
        self,
        session_id: str,
        constraint_name: str,
        value: str
    ) -> bool:
        """
        Update constraint value (e.g., user selected size).
        """
        mission = self.get_mission(session_id)
        if not mission:
            return False
        
        if constraint_name in mission.constraints:
            mission.constraints[constraint_name].value = value
            mission.constraints[constraint_name].status = ConstraintStatus.FILLED
            self._save(mission)
            
            logger.info(f"📝 Constraint updated: {constraint_name} = {value}")
            return True
        
        return False
    
    def record_action(self, session_id: str, action: str, outcome: str) -> None:
        """Add action to history."""
        mission = self.get_mission(session_id)
        if mission:
            entry = f"{action} → {outcome}"
            mission.action_history.append(entry)
            mission.action_history = mission.action_history[-20:]  # Keep last 20
            self._save(mission)
    
    def record_url(self, session_id: str, url: str) -> None:
        """Track visited URL."""
        mission = self.get_mission(session_id)
        if mission and url not in mission.visited_urls:
            mission.visited_urls.append(url)
            self._save(mission)
    
    def increment_ambiguity(self, session_id: str) -> int:
        """Increment ambiguity counter."""
        mission = self.get_mission(session_id)
        if mission:
            mission.ambiguity_count += 1
            self._save(mission)
            return mission.ambiguity_count
        return 0
    
    # ═══════════════════════════════════════════════════════════
    # LOGIC GUARDS (The Critical Part)
    # ═══════════════════════════════════════════════════════════
    
    def audit_action(
        self,
        session_id: str,
        proposed_target_id: str,
        proposed_target_text: str,
        strategy: Optional[StrategyHint]
    ) -> Tuple[bool, str]:
        """
        Audit a proposed action BEFORE execution.
        Returns (approved, reason).
        
        This is where constraint blocking happens.
        """
        mission = self.get_mission(session_id)
        if not mission:
            return (True, "No mission state")
        
        # Guard 1: Constraint Check
        if strategy and strategy.blocking_rules:
            for action_keyword, required_constraints in strategy.blocking_rules.items():
                # Check if proposed action matches a blocking rule
                if action_keyword.lower() in proposed_target_text.lower():
                    # Check if all required constraints are filled
                    for constraint_name in required_constraints:
                        if constraint_name in mission.constraints:
                            constraint = mission.constraints[constraint_name]
                            if constraint.status == ConstraintStatus.MISSING:
                                logger.warning(
                                    f"🛡️ BLOCKED: '{proposed_target_text}' requires "
                                    f"'{constraint_name}' to be filled"
                                )
                                return (
                                    False,
                                    f"Cannot {action_keyword} until {constraint_name} is selected"
                                )
        
        # Guard 2: Ambiguity Limit
        if mission.ambiguity_count >= 3:
            logger.warning(f"🛡️ BLOCKED: Too many ambiguous situations (count={mission.ambiguity_count})")
            return (False, "Too many ambiguous choices - need user clarification")
        
        # Guard 3: Sequence Enforcement (optional)
        # Could enforce that subgoals are completed in order
        # Currently disabled to allow flexibility
        
        return (True, "Approved")
    
    def should_ask_user(self, session_id: str, detective_report: DetectiveReport) -> bool:
        """
        Decide if we should trigger user clarification.
        Based on ambiguity and constraint status.
        """
        mission = self.get_mission(session_id)
        if not mission:
            return False
        
        # Ambiguity trap
        if detective_report.is_ambiguous and detective_report.ambiguous_count > 2:
            return True
        
        # Missing critical constraint
        missing = [c for c in mission.constraints.values() if c.status == ConstraintStatus.MISSING]
        if len(missing) > 0 and detective_report.recommended_action == 'click':
            # Check if clicking a "buy/submit/confirm" type button
            if detective_report.best_match:
                text_lower = detective_report.best_match.text.lower()
                critical_actions = {'buy', 'purchase', 'order', 'submit', 'confirm', 'add to cart'}
                if any(kw in text_lower for kw in critical_actions):
                    logger.warning(
                        f"🛡️ Should ask user: Missing constraints {[c.name for c in missing]} "
                        f"before critical action '{text_lower}'"
                    )
                    return True
        
        return False
    
    def get_current_subgoal(self, session_id: str) -> Optional[str]:
        """Get current sub-goal text."""
        mission = self.get_mission(session_id)
        if not mission:
            return None
        
        if 0 <= mission.current_subgoal_index < len(mission.subgoals):
            return mission.subgoals[mission.current_subgoal_index]
        
        return None
    
    def advance_subgoal(self, session_id: str) -> bool:
        """Move to next sub-goal."""
        mission = self.get_mission(session_id)
        if not mission:
            return False
        
        mission.current_subgoal_index += 1
        
        if mission.current_subgoal_index >= len(mission.subgoals):
            mission.status = "completed"
            logger.info(f"🏁 Mission completed: {mission.schema.target_entity}")
        else:
            logger.info(
                f"➡️  Advanced to sub-goal {mission.current_subgoal_index + 1}/{len(mission.subgoals)}: "
                f"{mission.subgoals[mission.current_subgoal_index]}"
            )
        
        self._save(mission)
        return True
    
    # ═══════════════════════════════════════════════════════════
    # SERIALIZATION
    # ═══════════════════════════════════════════════════════════
    
    def _serialize(self, mission: MissionState) -> dict:
        """Convert MissionState to Redis-compatible dict."""
        return {
            "mission_id": mission.mission_id,
            "session_id": mission.session_id,
            "schema": {
                "action": mission.schema.action.value,
                "target_entity": mission.schema.target_entity,
                "domain": mission.schema.domain,
                "constraints": mission.schema.constraints,
                "raw_utterance": mission.schema.raw_utterance,
                "timestamp": mission.schema.timestamp
            },
            "status": mission.status,
            "current_subgoal_index": mission.current_subgoal_index,
            "subgoals": mission.subgoals,
            "constraints": {
                k: {"name": v.name, "value": v.value, "status": v.status.value}
                for k, v in mission.constraints.items()
            },
            "visited_urls": mission.visited_urls,
            "action_history": mission.action_history,
            "ambiguity_count": mission.ambiguity_count,
            "created_at": mission.created_at,
            "updated_at": mission.updated_at
        }
    
    def _deserialize(self, data: dict) -> MissionState:
        """Convert dict back to MissionState."""
        from tara_models import ActionIntent
        
        schema_data = data["schema"]
        schema = TacticalSchema(
            action=ActionIntent(schema_data["action"]),
            target_entity=schema_data["target_entity"],
            domain=schema_data["domain"],
            constraints=schema_data["constraints"],
            raw_utterance=schema_data["raw_utterance"],
            timestamp=schema_data["timestamp"]
        )
        
        constraints = {}
        for k, v in data["constraints"].items():
            constraints[k] = Constraint(
                name=v["name"],
                value=v["value"],
                status=ConstraintStatus(v["status"])
            )
        
        return MissionState(
            mission_id=data["mission_id"],
            session_id=data["session_id"],
            schema=schema,
            status=data["status"],
            current_subgoal_index=data["current_subgoal_index"],
            subgoals=data["subgoals"],
            constraints=constraints,
            visited_urls=data["visited_urls"],
            action_history=data["action_history"],
            ambiguity_count=data["ambiguity_count"],
            created_at=data["created_at"],
            updated_at=data["updated_at"]
        )
Integration Point
Main orchestration flow becomes:
python# 1. Input
schema = await mind_reader.translate(user_input, url)

# 2. Memory
hive_response = await hive.retrieve(schema)

# 3. Mission Setup
mission = mission_brain.create_mission(session_id, schema, hive_response.strategy)

# 4. Perception
report = await detective.investigate(session_id, schema, hive_response.visual_hints, subgoal)

# 5. Logic Guard
approved, reason = mission_brain.audit_action(
    session_id,
    report.best_match.node_id if report.best_match else "",
    report.best_match.text if report.best_match else "",
    hive_response.strategy
)

# 6. Execution (only if approved)
if approved:
    execute_action(report.best_match.node_id, report.recommended_action)
else:
    ask_user(reason)

Phase 3: Migration Plan
Step 1: Add New Modules (Parallel)

Create tara_models.py
Add tara_sensor.js to widget (keep old scanPageBlueprint for now)
Implement live_graph.py
Test delta streaming in isolation

Step 2: Replace Input Processing

Implement mind_reader.py
Replace _decompose_goal calls with mind_reader.translate()
Verify schema generation works

Step 3: Integrate Hive

Implement hive_interface.py
Seed Qdrant with example Strategy_Sequence and Visual_Hint documents
Test dual retrieval

Step 4: Replace Detective

Implement semantic_detective.py
Side-by-side test with current detective.py
Compare outputs, tune weights

Step 5: Add Mission Brain

Implement mission_brain.py
Add audit hooks to execution flow
Test constraint blocking

Step 6: Deprecate Old Code

Remove old detective.py
Remove scanPageBlueprint from widget
Clean up visual_orchestrator.py (becomes thin coordination layer)


Phase 4: Testing Strategy
Unit Tests
Each module gets isolated tests:
python# test_mind_reader.py
async def test_purchase_intent():
    schema = await mind_reader.translate("Buy a white shirt", "https://shop.com")
    assert schema.action == ActionIntent.PURCHASE
    assert "color" in schema.constraints
    assert schema.constraints["color"] == "white"

# test_semantic_detective.py
async def test_hive_hint_boost():
    # Create mock nodes and hints
    # Verify hint match gives score boost

# test_mission_brain.py
def test_constraint_blocking():
    # Setup mission with missing "size"
    # Verify "Add to Cart" is blocked
Integration Tests
Test full pipeline:
pythonasync def test_e2e_purchase_flow():
    """
    Simulate: "Buy a medium white shirt"
    Expected: 
    - Schema extracts size=medium, color=white
    - Hive returns strategy with "Select Size" before "Add to Cart"
    - Mission Brain blocks "Add to Cart" until size selected
    """
A/B Testing
Run new system in shadow mode:

Current system executes
New system runs in parallel
Compare decisions


Summary
This transformation gives you:
✅ Zero Ambiguity Input - Structured schemas replace raw strings
✅ Memory-Guided Navigation - Hive provides both strategy AND visual cheats
✅ Live DOM Mirror - Millisecond-accurate graph, no browser lag
✅ Hybrid Target Lock - Semantic meaning + learned selectors
✅ Constraint Enforcement - Logic guard prevents premature actions
The architecture is modular - each file has a single responsibility and clear interfaces. You can implement them one at a time and test incrementally.
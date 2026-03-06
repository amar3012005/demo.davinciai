"""
semantic_detective.py

PURPOSE: Hybrid scoring engine for element detection.
         Combines semantic similarity (vector embeddings) with
         hive hints (Qdrant-retrieved selectors) to score and
         rank candidate elements.

DEPENDENCIES:
    - sentence_transformers: For semantic similarity scoring
    - numpy: For cosine similarity calculations
    - tara_models: GraphNode, VisualHint, ScoredCandidate, DetectiveReport
    - live_graph: LiveGraph for querying DOM nodes

USED BY:
    - mission_brain.py: Gets candidate elements for action selection
    - visual_orchestrator.py: Gets detective report for decision making

MIGRATION STATUS: [NEW] - Decision layer for Ultimate TARA

SCORING ALGORITHM:
    hybrid_score = (semantic_score * 0.6) + (hive_score * 0.4)
    
    Where:
    - semantic_score: Cosine similarity between query and element text
    - hive_score: Match quality with Qdrant-retrieved hints

ERROR HANDLING:
    - Model loading failure → Falls back to keyword matching
    - No candidates found → Returns empty report with low confidence
    - Embedding failure → Uses keyword-based scoring

Example:
    from semantic_detective import SemanticDetective
    from live_graph import LiveGraph
    
    detective = SemanticDetective(live_graph)
    
    report = await detective.investigate(
        session_id="session-123",
        query="export button",
        hive_hints=[visual_hint1, visual_hint2]
    )
    
    print(report.best_match.node_id)  # "tara-abc123"
    print(report.best_match.hybrid_score)  # 0.87
    print(report.confidence)  # "high"
"""

import logging
import time
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass

from tara_models import (
    GraphNode, VisualHint, ScoredCandidate, DetectiveReport, ActionIntent
)

# Optional imports with graceful fallback
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    SentenceTransformer = None

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    np = None

logger = logging.getLogger(__name__)


class SemanticDetective:
    """
    Hybrid scoring engine for element detection.
    
    Combines:
    1. Semantic similarity (60% weight) - Vector embeddings
    2. Hive hints (40% weight) - Qdrant-retrieved selectors
    
    Attributes:
        live_graph: LiveGraph instance for DOM queries
        model: Sentence transformer model (or None if unavailable)
        semantic_weight: Weight for semantic score (default 0.6)
        hive_weight: Weight for hive score (default 0.4)
    """

    def __init__(
        self,
        live_graph: Any,
        model_name: str = "all-MiniLM-L6-v2",
        semantic_weight: float = 0.6,
        hive_weight: float = 0.4,
        embeddings: Optional[Any] = None
    ):
        """
        Initialize SemanticDetective.

        Args:
            live_graph: LiveGraph instance for DOM queries
            model_name: Sentence transformer model name (used if embeddings is None)
            semantic_weight: Weight for semantic score
            hive_weight: Weight for hive score
            embeddings: Pre-loaded embeddings object (OptimizedEmbeddings or SentenceTransformer)
        """
        self.live_graph = live_graph
        self.semantic_weight = semantic_weight
        self.hive_weight = hive_weight

        # Load model (with fallback)
        self.model = None
        self._model_available = False
        self._shared_embeddings = None

        if embeddings is not None:
            self.model = embeddings
            self._model_available = True
            logger.info("🔍 SemanticDetective using provided embeddings instance")
        else:
            try:
                from remote_embeddings import RemoteEmbeddings
                self.model = RemoteEmbeddings()
                self._model_available = True
                logger.info("🔍 SemanticDetective using RemoteEmbeddings via microservice")
            except Exception as e:
                logger.warning(f"Failed to load RemoteEmbeddings: {e}")

        logger.info(
            f"🔍 SemanticDetective initialized: "
            f"semantic={semantic_weight}, hive={hive_weight}"
        )

    async def investigate(
        self,
        session_id: str,
        query: str,
        hive_hints: Optional[List[VisualHint]] = None,
        top_k: int = 10,
        action_intent: Optional[ActionIntent] = None,
        excluded_ids: Optional[List[str]] = None
    ) -> DetectiveReport:
        """
        Investigate DOM to find best matching elements.
        
        Args:
            session_id: Session identifier
            query: Search query (e.g., "export button", "search input")
            hive_hints: Optional visual hints from Qdrant
            top_k: Number of top candidates to return
            action_intent: Optional action intent for context
        
        Returns:
            DetectiveReport with scored candidates
        """
        start_time = time.time()
        
        # Get visible nodes from LiveGraph
        nodes = await self.live_graph.get_visible_nodes(session_id)
        
        if not nodes:
            logger.warning(f"No nodes found for session {session_id}")
            return self._empty_report()
        
        # Filter to interactive nodes
        interactive_nodes = [n for n in nodes if n.interactive]
        
        if not interactive_nodes:
            logger.warning(f"No interactive nodes found for session {session_id}")
            return self._empty_report()
        
        # Build set of already-tried element IDs for penalty
        _excluded = set(excluded_ids or [])
        if _excluded:
            logger.info(f"🔍 Detective excluding {len(_excluded)} already-tried IDs: {list(_excluded)[:5]}")

        # ═══════════════════════════════════════════════════════════════
        # BATCH EMBEDDING: Pre-compute query + all node embeddings at once
        # This eliminates the N×2 embedding bottleneck (was 4.8s for 161 nodes)
        # ═══════════════════════════════════════════════════════════════
        query_vector = None
        node_vectors = {}  # node.id → vector
        
        if self._model_available and NUMPY_AVAILABLE:
            try:
                # 1. Embed the query ONCE
                if self.model is not None:
                    if hasattr(self.model, 'encode'):
                        # Using our RemoteEmbeddings or standard SentenceTransformer with encode
                        query_kwargs = {}
                        if not hasattr(self.model, 'endpoint_url'): # Not our wrapper
                            query_kwargs = {"show_progress_bar": False, "convert_to_numpy": True}
                        query_vector = self.model.encode([query], **query_kwargs)[0]
                    elif hasattr(self.model, 'embed_query'):
                        query_vector = np.array(self.model.embed_query(query))
                
                # 2. Batch-embed ALL node texts in a single call
                all_texts = [n.text or "" for n in interactive_nodes]
                if self.model is not None:
                    if hasattr(self.model, 'encode'):
                        encode_kwargs = {}
                        if not hasattr(self.model, 'endpoint_url'): # Not our wrapper
                            encode_kwargs = {"show_progress_bar": False, "convert_to_numpy": True}
                        all_embeddings = self.model.encode(all_texts, **encode_kwargs)
                        for i, node in enumerate(interactive_nodes):
                            node_vectors[node.id] = all_embeddings[i]
                    elif hasattr(self.model, 'embed_documents'):
                        all_embeddings = self.model.embed_documents(all_texts)
                        for i, node in enumerate(interactive_nodes):
                            node_vectors[node.id] = np.array(all_embeddings[i])
                    elif hasattr(self.model, 'embed_query'):
                        all_embeddings = [self.model.embed_query(t) for t in all_texts]
                        for i, node in enumerate(interactive_nodes):
                            node_vectors[node.id] = np.array(all_embeddings[i])
                
                logger.debug(f"🔍 Batch embedded: 1 query + {len(all_texts)} nodes")
            except Exception as e:
                logger.warning(f"Batch embedding failed, falling back to keyword scoring: {e}")
                query_vector = None
                node_vectors = {}
        
        # Score all candidates (using pre-computed vectors)
        scored_candidates = []

        for node in interactive_nodes:
            candidate = self._score_candidate(
                node, query, hive_hints or [],
                query_vector=query_vector,
                node_vector=node_vectors.get(node.id)
            )
            # Heavily penalize already-tried elements (anti-loop)
            if candidate.node_id in _excluded:
                candidate.hybrid_score *= 0.1
                candidate.reasons.append("already_tried_penalty")
            if candidate.hybrid_score > 0.1:  # Threshold
                scored_candidates.append(candidate)
        
        # Sort by hybrid score (descending)
        scored_candidates.sort(key=lambda c: c.hybrid_score, reverse=True)
        
        # Take top K
        top_candidates = scored_candidates[:top_k]
        
        # Detect ambiguity
        is_ambiguous = self._detect_ambiguity(top_candidates)
        ambiguous_count = sum(
            1 for c in top_candidates
            if c.hybrid_score >= (top_candidates[0].hybrid_score - 0.1)
        ) if top_candidates else 0
        
        # Detect obstacles
        has_obstacle, obstacle_type, dismiss_id = await self._detect_obstacles(
            session_id, nodes
        )
        
        # Determine page type
        page_type = self._classify_page(nodes, action_intent)
        
        # Determine recommended action
        best_match = top_candidates[0] if top_candidates else None
        recommended_action = self._determine_action(
            best_match, has_obstacle, action_intent, dismiss_id
        )
        
        # Determine confidence
        confidence = self._determine_confidence(
            best_match, is_ambiguous, len(top_candidates)
        )
        
        report = DetectiveReport(
            candidates=top_candidates,
            best_match=best_match,
            is_ambiguous=is_ambiguous,
            ambiguous_count=ambiguous_count,
            has_obstacle=has_obstacle,
            obstacle_type=obstacle_type,
            dismiss_button_id=dismiss_id,
            page_type=page_type,
            recommended_action=recommended_action,
            confidence=confidence
        )
        
        elapsed_ms = int((time.time() - start_time) * 1000)

        # Log top-3 candidates with score breakdown
        for i, c in enumerate(top_candidates[:3]):
            logger.info(
                f"🔍 Detective #{i+1}: '{c.text[:40]}' "
                f"[hybrid={c.hybrid_score:.2f} sem={c.semantic_score:.2f} hive={c.hive_score:.2f}] "
                f"tag={c.tag} zone={c.zone}"
            )

        logger.info(
            f"🔍 Detective: '{query}' → "
            f"best='{best_match.text if best_match else 'none'}' "
            f"(score={best_match.hybrid_score if best_match else 0:.2f}, "
            f"confidence={confidence}, {len(scored_candidates)} candidates, {elapsed_ms}ms)"
        )

        return report

    def _score_candidate(
        self,
        node: GraphNode,
        query: str,
        hive_hints: List[VisualHint],
        query_vector: Any = None,
        node_vector: Any = None
    ) -> ScoredCandidate:
        """
        Score a single candidate element.
        
        Args:
            node: GraphNode to score
            query: Search query
            hive_hints: Visual hints from Qdrant
            query_vector: Pre-computed query embedding (avoids re-embedding)
            node_vector: Pre-computed node text embedding (avoids re-embedding)
        
        Returns:
            ScoredCandidate with scores
        """
        # Calculate semantic score (using pre-computed vectors if available)
        semantic_score = self._calculate_semantic_score(
            node, query,
            query_vector=query_vector,
            node_vector=node_vector
        )
        
        # Calculate hive score
        hive_score, matched_hint = self._calculate_hive_score(node, hive_hints)
        
        # Calculate hybrid score
        hybrid_score = (
            semantic_score * self.semantic_weight +
            hive_score * self.hive_weight
        )
        
        # Build reasons list
        reasons = []
        if semantic_score > 0.5:
            reasons.append(f"semantic_match:{semantic_score:.2f}")
        if hive_score > 0.5:
            reasons.append(f"hive_match:{hive_score:.2f}")
        if node.in_viewport if hasattr(node, 'in_viewport') else True:
            reasons.append("in_viewport")
        if node.zone == "nav":
            reasons.append("navigation_zone")
        
        return ScoredCandidate(
            node_id=node.id,
            text=node.text,
            tag=node.tag,
            zone=node.zone,
            semantic_score=semantic_score,
            hive_score=hive_score,
            hybrid_score=hybrid_score,
            matched_hint=matched_hint,
            reasons=reasons
        )

    def _calculate_semantic_score(
        self,
        node: GraphNode,
        query: str,
        query_vector: Any = None,
        node_vector: Any = None
    ) -> float:
        """
        Calculate semantic similarity between query and node.
        
        Uses pre-computed batch vectors if available (fast path),
        otherwise falls back to individual embedding (slow path).
        
        Args:
            node: GraphNode to score
            query: Search query
            query_vector: Pre-computed query embedding (from batch)
            node_vector: Pre-computed node embedding (from batch)
        
        Returns:
            Semantic score (0.0 - 1.0)
        """
        if not self._model_available or not NUMPY_AVAILABLE:
            return self._keyword_score(node, query)
        
        try:
            # FAST PATH: Use pre-computed vectors from batch embedding
            if query_vector is not None and node_vector is not None:
                similarity = self._cosine_similarity(query_vector, node_vector)
                return float(similarity)
            
            # SLOW PATH (fallback): Compute individually
            if hasattr(self.model, 'encode'):
                kwargs = {}
                if not hasattr(self.model, 'endpoint_url'): # Not our wrapper
                    kwargs = {"show_progress_bar": False, "convert_to_numpy": True}
                query_embedding = self.model.encode([query], **kwargs)[0]
                node_embedding = self.model.encode([node.text or ""], **kwargs)[0]
            elif hasattr(self.model, 'embed_query'):
                query_embedding = np.array(self.model.embed_query(query))
                node_embedding = np.array(self.model.embed_query(node.text or ""))
                
            similarity = self._cosine_similarity(query_embedding, node_embedding)
            return float(similarity)
            
        except Exception as e:
            logger.warning(f"Semantic scoring failed: {e}")
            return self._keyword_score(node, query)

    def _keyword_score(self, node: GraphNode, query: str) -> float:
        """
        Fallback keyword-based scoring.
        
        Args:
            node: GraphNode to score
            query: Search query
        
        Returns:
            Keyword score (0.0 - 1.0)
        """
        query_lower = query.lower()
        node_text_lower = node.text.lower()
        
        # Exact match
        if query_lower == node_text_lower:
            return 1.0
        
        # Contains query
        if query_lower in node_text_lower:
            return 0.8
        
        # Word overlap
        query_words = set(query_lower.split())
        node_words = set(node_text_lower.split())
        
        overlap = query_words & node_words
        if overlap:
            return min(0.3 + (len(overlap) / len(query_words)) * 0.5, 0.7)
        
        # Partial match
        for word in query_words:
            if len(word) > 3 and word in node_text_lower:
                return 0.5
        
        return 0.1

    def _calculate_hive_score(
        self,
        node: GraphNode,
        hive_hints: List[VisualHint]
    ) -> Tuple[float, Optional[VisualHint]]:
        """
        Calculate hive hint match score.
        
        Args:
            node: GraphNode to score
            hive_hints: Visual hints from Qdrant
        
        Returns:
            Tuple of (hive_score, matched_hint)
        """
        if not hive_hints:
            return 0.0, None
        
        best_score = 0.0
        best_hint = None
        
        for hint in hive_hints:
            score = 0.0
            
            # Selector match
            if hint.selector:
                if hint.selector == node.id:
                    score = max(score, 1.0)
                elif hint.selector in node.id:
                    score = max(score, 0.8)
                elif self._selector_matches(node, hint.selector):
                    score = max(score, 0.7)
            
            # Element type match
            if hint.element_type:
                if hint.element_type == node.tag:
                    score = max(score, 0.5)
            
            # Zone match
            if hint.zone == node.zone:
                score = max(score, 0.3)
            
            # Text pattern match
            if hint.text_pattern:
                import re
                try:
                    if re.search(hint.text_pattern, node.text, re.IGNORECASE):
                        score = max(score, 0.6)
                except re.error:
                    pass
            
            if score > best_score:
                best_score = score
                best_hint = hint
        
        return best_score, best_hint

    def _selector_matches(self, node: GraphNode, selector: str) -> bool:
        """
        Check if selector matches node.
        
        Args:
            node: GraphNode
            selector: CSS selector or pattern
        
        Returns:
            True if matches
        """
        # ID selector
        if selector.startswith('#'):
            return selector[1:] in node.id
        
        # Class selector
        if selector.startswith('.'):
            return False  # Can't check classes from GraphNode
        
        # Tag selector
        if selector == node.tag:
            return True
        
        # Attribute selector
        import re
        attr_match = re.match(r'\[(\w+)=[\'"]?(\w+)[\'"]?\]', selector)
        if attr_match:
            attr_name, attr_value = attr_match.groups()
            return getattr(node, attr_name, '') == attr_value
        
        return False

    def _cosine_similarity(self, a: Any, b: Any) -> float:
        """
        Calculate cosine similarity between two vectors.
        
        Args:
            a: First vector
            b: Second vector
        
        Returns:
            Cosine similarity (0.0 - 1.0)
        """
        if not NUMPY_AVAILABLE or np is None:
            return 0.0
        
        dot_product = np.dot(a, b)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        
        if norm_a == 0 or norm_b == 0:
            return 0.0
        
        return float(dot_product / (norm_a * norm_b))

    def _detect_ambiguity(self, candidates: List[ScoredCandidate]) -> bool:
        """
        Detect if multiple candidates have similar scores.
        
        Args:
            candidates: List of scored candidates
        
        Returns:
            True if ambiguous
        """
        if len(candidates) < 2:
            return False
        
        top_score = candidates[0].hybrid_score
        second_score = candidates[1].hybrid_score
        
        # Ambiguous if within 0.1 score
        return (top_score - second_score) < 0.1

    async def _detect_obstacles(
        self,
        session_id: str,
        nodes: List[GraphNode]
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Detect modals, cookie banners, overlays.
        
        Args:
            session_id: Session identifier
            nodes: List of graph nodes
        
        Returns:
            Tuple of (has_obstacle, obstacle_type, dismiss_button_id)
        """
        # Only detect TRUE overlays: nodes explicitly in the "modal" zone
        # Do NOT scan all nodes for cookie/privacy keywords — footer links
        # like "Privacy Policy" cause false positives on every page.
        modal_nodes = [n for n in nodes if n.zone == "modal"]
        if modal_nodes:
            # Look for dismiss button in the modal
            dismiss_keywords = ["close", "dismiss", "x", "cancel", "accept", "got it"]
            for node in modal_nodes:
                for keyword in dismiss_keywords:
                    if keyword in node.text.lower():
                        return True, "modal", node.id

            return True, "modal", None

        return False, None, None

    def _classify_page(
        self,
        nodes: List[GraphNode],
        action_intent: Optional[ActionIntent]
    ) -> str:
        """
        Classify page type based on nodes.
        
        Args:
            nodes: List of graph nodes
            action_intent: Optional action intent
        
        Returns:
            Page type string
        """
        # Count element types
        input_count = sum(1 for n in nodes if n.tag in ('input', 'textarea', 'select'))
        button_count = sum(1 for n in nodes if n.tag == 'button')
        link_count = sum(1 for n in nodes if n.tag == 'a')
        
        # Check for search-related elements
        search_keywords = ["search", "find", "lookup", "query"]
        has_search = any(
            any(kw in n.text.lower() for kw in search_keywords)
            for n in nodes
        )
        
        if input_count >= 3:
            return "form"
        elif has_search or (input_count == 1 and button_count >= 1):
            return "search"
        elif link_count > button_count * 2:
            return "nav"
        elif action_intent == ActionIntent.EXTRACTION:
            return "data"
        else:
            return "general"

    def _determine_action(
        self,
        best_match: Optional[ScoredCandidate],
        has_obstacle: bool,
        action_intent: Optional[ActionIntent],
        dismiss_id: Optional[str] = None
    ) -> str:
        """
        Determine recommended action.

        Args:
            best_match: Best matching candidate
            has_obstacle: Whether obstacle detected
            action_intent: Optional action intent
            dismiss_id: Dismiss button ID if obstacle found

        Returns:
            Recommended action string
        """
        if has_obstacle and dismiss_id:
            return "dismiss"

        if not best_match:
            return "scroll"
        
        # Map action intent to action
        if action_intent == ActionIntent.INTERACTION:
            return "click"
        elif action_intent == ActionIntent.NAVIGATION:
            return "click"
        elif action_intent == ActionIntent.PURCHASE:
            return "click"
        elif action_intent == ActionIntent.SEARCH:
            if best_match.tag in ('input', 'textarea'):
                return "type"
            return "click"
        elif action_intent == ActionIntent.EXTRACTION:
            return "click"
        
        # Default based on element type
        if best_match.tag in ('input', 'textarea'):
            return "type"
        elif best_match.tag == 'select':
            return "select"
        else:
            return "click"

    def _determine_confidence(
        self,
        best_match: Optional[ScoredCandidate],
        is_ambiguous: bool,
        candidate_count: int
    ) -> str:
        """
        Determine confidence level.
        
        Args:
            best_match: Best matching candidate
            is_ambiguous: Whether multiple candidates
            candidate_count: Number of candidates
        
        Returns:
            Confidence string: "high", "medium", or "low"
        """
        if not best_match:
            return "low"
        
        if best_match.hybrid_score >= 0.8 and not is_ambiguous:
            return "high"
        elif best_match.hybrid_score >= 0.6:
            return "medium"
        elif best_match.hybrid_score >= 0.4:
            return "medium"
        else:
            return "low"

    def _empty_report(self) -> DetectiveReport:
        """
        Create empty report when no candidates found.
        
        Returns:
            Empty DetectiveReport
        """
        return DetectiveReport(
            candidates=[],
            best_match=None,
            is_ambiguous=False,
            ambiguous_count=0,
            has_obstacle=False,
            obstacle_type=None,
            dismiss_button_id=None,
            page_type="unknown",
            recommended_action="scroll",
            confidence="low"
        )


# ═══════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ═══════════════════════════════════════════════════════════════

def create_semantic_detective(
    live_graph: Any,
    model_name: str = "all-MiniLM-L6-v2"
) -> SemanticDetective:
    """
    Factory function to create SemanticDetective.
    
    Args:
        live_graph: LiveGraph instance
        model_name: Sentence transformer model name
    
    Returns:
        SemanticDetective instance
    """
    return SemanticDetective(live_graph, model_name=model_name)

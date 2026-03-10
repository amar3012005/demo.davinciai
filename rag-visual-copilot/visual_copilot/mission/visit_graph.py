#!/usr/bin/env python3
"""
Visit Graph v1 - Phase 2 Implementation
Lightweight append-only visit log for mission-scoped cross-page memory.

This module provides:
- VisitRecord: Append-only mission-scoped visit records
- EvidenceEntry: Compact evidence entries for the vault
- VisitGraphV1: Core visit graph management with auto-linking and evidence accumulation

Feature Flags:
- LAST_MILE_VISIT_GRAPH_V1_ENABLED: Master toggle for visit graph functionality
- LAST_MILE_VISIT_EVIDENCE_VAULT_ENABLED: Toggle for evidence vault features
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Feature Flags
LAST_MILE_VISIT_GRAPH_V1_ENABLED = os.getenv(
    "LAST_MILE_VISIT_GRAPH_V1_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}

LAST_MILE_VISIT_EVIDENCE_VAULT_ENABLED = os.getenv(
    "LAST_MILE_VISIT_EVIDENCE_VAULT_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class EvidenceEntry:
    """Compact evidence entry for vault.
    
    Attributes:
        excerpt: The actual text/content found
        source_label: Human-readable label (e.g., "Usage Table", "Cost Summary")
        confidence: Confidence score (0.0-1.0)
        timestamp: When this evidence was captured
        goal_relevance_score: How relevant to mission goal (0.0-1.0)
    """
    excerpt: str
    source_label: str
    confidence: float
    timestamp: float
    goal_relevance_score: float


@dataclass
class VisitRecord:
    """Append-only mission-scoped visit record.
    
    This record captures a single page visit within a mission context,
    including evidence found, page state, and navigation relationships.
    
    Attributes:
        visit_id: Unique identifier for this visit
        mission_id: Parent mission identifier
        parent_visit_id: Previous visit in the chain (None for root)
        url: Full URL of the visited page
        domain: Extracted domain from URL
        page_title: Title of the page
        page_node_id: Reference to site_map node if available
        entered_at: Unix timestamp when visit started
        exit_action: What action led to leaving (e.g., "click", "redirect", "back")
        evidence_vault: List of evidence entries found on this page
        evidence_score: Accumulated evidence score for this visit
        semantic_summary: Brief description of what was found
        page_state_snapshot: Full page state from page_state module
    """
    visit_id: str
    mission_id: str
    parent_visit_id: Optional[str]
    url: str
    domain: str
    page_title: str
    page_node_id: Optional[str] = None
    entered_at: float = field(default_factory=time.time)
    exit_action: Optional[str] = None
    evidence_vault: List[Dict[str, Any]] = field(default_factory=list)
    evidence_score: float = 0.0
    semantic_summary: str = ""
    page_state_snapshot: Optional[Any] = None


class VisitGraphV1:
    """Lightweight append-only visit log for mission-scoped cross-page memory.
    
    The VisitGraph maintains a tree of page visits within a mission context,
    enabling:
    - Cross-page memory (what was found on previous pages)
    - Smart backtracking (return to high-evidence pages)
    - Breadcrumb navigation (path from root to current)
    - Evidence accumulation (per-visit and mission-wide)
    
    Key Behaviors:
    - Append-Only: Visits are never deleted, only marked with exit_action
    - Auto-Linking: New visits automatically link to current as parent
    - Evidence Accumulation: Each visit tracks relevant evidence
    - Smart Creation: Only creates visits for real page transitions
    
    Example:
        >>> graph = VisitGraphV1("mission_123")
        >>> visit1 = graph.create_visit("https://console.groq.com", "Groq Console")
        >>> graph.add_evidence("API key: gsk_...", "API Keys Section", 0.95, 0.9)
        >>> visit2 = graph.create_visit("https://console.groq.com/billing", "Billing")
        >>> breadcrumb = graph.get_breadcrumb()
        >>> backtrack = graph.suggest_backtrack()
    """
    
    def __init__(self, mission_id: str, redis_client: Optional[Any] = None):
        """Initialize visit graph for a mission.
        
        Args:
            mission_id: Unique identifier for the mission
            redis_client: Optional Redis client for persistence
        """
        self.mission_id = mission_id
        self.visits: Dict[str, VisitRecord] = {}
        self.current_visit_id: Optional[str] = None
        self.root_visit_id: Optional[str] = None
        self.redis = redis_client
    
    def create_visit(
        self,
        url: str,
        page_title: str,
        page_node_id: Optional[str] = None,
        page_state: Optional[Any] = None,
    ) -> VisitRecord:
        """Create new visit record. Auto-link to current visit as parent.
        
        This is the primary method for recording a new page visit. It:
        1. Generates a unique visit ID
        2. Links to the current visit as parent (if exists)
        3. Extracts domain from URL
        4. Sets the new visit as current
        5. Tracks root visit for breadcrumb generation
        
        Args:
            url: Full URL of the page being visited
            page_title: Title of the page
            page_node_id: Optional reference to site_map node
            page_state: Optional page state snapshot
            
        Returns:
            The newly created VisitRecord
            
        Example:
            >>> graph = VisitGraphV1("mission_123")
            >>> visit = graph.create_visit("https://example.com", "Example Page")
            >>> print(visit.visit_id)
            'visit_a1b2c3d4e5f6'
        """
        visit_id = f"visit_{uuid.uuid4().hex[:12]}"
        parent_id = self.current_visit_id
        
        visit = VisitRecord(
            visit_id=visit_id,
            mission_id=self.mission_id,
            parent_visit_id=parent_id,
            url=url,
            domain=urlparse(url).netloc,
            page_title=page_title,
            page_node_id=page_node_id,
            entered_at=time.time(),
            exit_action=None,
            evidence_vault=[],
            evidence_score=0.0,
            semantic_summary="",
            page_state_snapshot=page_state,
        )
        
        self.visits[visit_id] = visit
        self.current_visit_id = visit_id
        
        if not self.root_visit_id:
            self.root_visit_id = visit_id
            
        return visit
    
    def should_create_new_visit(
        self,
        current_url: str,
        page_state_delta: Optional[Any] = None,
        domain_changed: bool = False,
    ) -> bool:
        """Determine if we should create a new visit record.
        
        This method implements smart visit creation logic to avoid
        creating visits for non-navigational changes like dropdowns
        or loading states.
        
        Create new visit when:
        - URL changed (different page)
        - Domain changed (cross-domain navigation)
        - Structure changed significantly (semantic classification)
        
        Don't create for:
        - Dropdown opens (interactives_changed only)
        - Loading interstitials
        - Small content-only refreshes
        - Same URL with minor DOM changes
        
        Args:
            current_url: The URL being navigated to
            page_state_delta: Optional delta from page state module
            domain_changed: Whether domain changed from previous
            
        Returns:
            True if a new visit should be created
            
        Example:
            >>> graph = VisitGraphV1("mission_123")
            >>> # Same URL, minor change - don't create
            >>> graph.should_create_new_visit("https://example.com", minor_delta)
            False
            >>> # Different URL - create new visit
            >>> graph.should_create_new_visit("https://example.com/page2", None)
            True
        """
        # No current visit - always create first visit
        if not self.current_visit_id:
            return True
        
        current_visit = self.visits.get(self.current_visit_id)
        if not current_visit:
            return True
        
        # Domain changed - definitely a new visit
        if domain_changed:
            return True
        
        # URL changed - new visit
        if current_visit.url != current_url:
            return True
        
        # Check page state delta for structural changes
        if page_state_delta:
            # If delta indicates structure_changed, create new visit
            if hasattr(page_state_delta, 'structure_changed') and page_state_delta.structure_changed:
                return True
            if isinstance(page_state_delta, dict) and page_state_delta.get('structure_changed'):
                return True
        
        # Same URL, no structural change - don't create new visit
        return False
    
    def add_evidence(
        self,
        excerpt: str,
        source_label: str,
        confidence: float,
        goal_relevance_score: float,
    ) -> None:
        """Add evidence to current visit's vault.
        
        Evidence entries are compact records of mission-relevant
        information found on the current page.
        
        Args:
            excerpt: The actual text/content found
            source_label: Human-readable source label
            confidence: Confidence score (0.0-1.0)
            goal_relevance_score: Relevance to mission goal (0.0-1.0)
            
        Raises:
            RuntimeError: If no current visit exists
            
        Example:
            >>> graph.add_evidence(
            ...     "API Key: gsk_abc123",
            ...     "API Keys Section",
            ...     0.95,
            ...     0.9
            ... )
        """
        if not LAST_MILE_VISIT_EVIDENCE_VAULT_ENABLED:
            return
            
        if not self.current_visit_id:
            raise RuntimeError("Cannot add evidence: no current visit")
        
        visit = self.visits.get(self.current_visit_id)
        if not visit:
            raise RuntimeError(f"Current visit {self.current_visit_id} not found")
        
        entry = EvidenceEntry(
            excerpt=excerpt,
            source_label=source_label,
            confidence=confidence,
            timestamp=time.time(),
            goal_relevance_score=goal_relevance_score,
        )
        
        visit.evidence_vault.append({
            "excerpt": entry.excerpt,
            "source_label": entry.source_label,
            "confidence": entry.confidence,
            "timestamp": entry.timestamp,
            "goal_relevance_score": entry.goal_relevance_score,
        })
        
        # Update evidence score based on relevance and confidence
        score_delta = confidence * goal_relevance_score
        self.update_evidence_score(score_delta)
    
    def update_evidence_score(self, delta: float) -> None:
        """Update current visit's accumulated evidence score.
        
        The evidence score accumulates based on confidence and
        relevance of evidence found on the page.
        
        Args:
            delta: Amount to add to the score
            
        Raises:
            RuntimeError: If no current visit exists
        """
        if not self.current_visit_id:
            raise RuntimeError("Cannot update score: no current visit")
        
        visit = self.visits.get(self.current_visit_id)
        if visit:
            visit.evidence_score += delta
    
    def get_current_visit(self) -> Optional[VisitRecord]:
        """Get current visit record.
        
        Returns:
            The current VisitRecord, or None if no visits exist
        """
        if not self.current_visit_id:
            return None
        return self.visits.get(self.current_visit_id)
    
    def get_parent_visit(self) -> Optional[VisitRecord]:
        """Get parent of current visit.
        
        Returns:
            The parent VisitRecord, or None if current is root
        """
        current = self.get_current_visit()
        if not current or not current.parent_visit_id:
            return None
        return self.visits.get(current.parent_visit_id)
    
    def get_highest_evidence_visit(self) -> Optional[VisitRecord]:
        """Get visit with highest evidence score.
        
        This is useful for backtracking suggestions when
        the current page has low evidence value.
        
        Returns:
            The VisitRecord with highest evidence_score, or None if no visits
        """
        if not self.visits:
            return None
        
        return max(self.visits.values(), key=lambda v: v.evidence_score)
    
    def get_latest_same_domain_visit(self, domain: str) -> Optional[VisitRecord]:
        """Get most recent visit to same domain.
        
        Args:
            domain: Domain to match (e.g., "console.groq.com")
            
        Returns:
            Most recent VisitRecord to that domain, or None
        """
        matching = [
            v for v in self.visits.values()
            if v.domain == domain and v.entered_at
        ]
        
        if not matching:
            return None
        
        return max(matching, key=lambda v: v.entered_at)
    
    def get_breadcrumb(self) -> List[VisitRecord]:
        """Get path from root to current visit.
        
        Returns:
            List of VisitRecords from root to current (inclusive)
        """
        breadcrumb = []
        current = self.get_current_visit()
        
        # Build path backwards
        while current:
            breadcrumb.append(current)
            if current.parent_visit_id:
                current = self.visits.get(current.parent_visit_id)
            else:
                current = None
        
        # Reverse to get root -> current order
        breadcrumb.reverse()
        return breadcrumb
    
    def mark_exit(self, action: str) -> None:
        """Mark current visit as exited via action.
        
        This is one of the few mutable operations on visits,
        recording how the user left the page.
        
        Args:
            action: Exit action (e.g., "click", "redirect", "back", "forward")
            
        Raises:
            RuntimeError: If no current visit exists
        """
        if not self.current_visit_id:
            raise RuntimeError("Cannot mark exit: no current visit")
        
        visit = self.visits.get(self.current_visit_id)
        if visit:
            visit.exit_action = action
    
    def suggest_backtrack(self) -> Optional[VisitRecord]:
        """Suggest backtrack target when stuck.
        
        Returns the highest-evidence visit if current evidence is weak.
        This helps recover from navigation dead-ends or low-value pages.
        
        Returns:
            Suggested VisitRecord to backtrack to, or None if no better option
            
        Example:
            >>> stuck = graph.suggest_backtrack()
            >>> if stuck:
            ...     print(f"Consider going back to: {stuck.page_title}")
        """
        current = self.get_current_visit()
        if not current:
            return None
        
        # If current has good evidence, no need to backtrack
        if current.evidence_score >= 0.5:
            return None
        
        # Find highest evidence visit
        best = self.get_highest_evidence_visit()
        if not best or best.visit_id == current.visit_id:
            return None
        
        # Only suggest if it's significantly better
        if best.evidence_score > current.evidence_score + 0.3:
            return best
        
        return None
    
    def to_context_string(self, max_visits: int = 5) -> str:
        """Format visit graph as compact context for prompts.
        
        Creates a human-readable summary of recent visits and
        evidence for inclusion in LLM prompts.
        
        Args:
            max_visits: Maximum number of recent visits to include
            
        Returns:
            Formatted context string
            
        Example:
            >>> context = graph.to_context_string()
            >>> print(context)
            Visit History (3 pages):
            1. Groq Console (console.groq.com) - Evidence: 1.2
               - Found: API key in API Keys Section
            2. Billing (console.groq.com/billing) - Evidence: 0.0
            Current: Usage (console.groq.com/usage) - Evidence: 0.0
        """
        if not self.visits:
            return "No visit history."
        
        lines = []
        breadcrumb = self.get_breadcrumb()
        
        # Show recent visits
        recent = breadcrumb[-max_visits:] if len(breadcrumb) > max_visits else breadcrumb
        
        lines.append(f"Visit History ({len(breadcrumb)} pages):")
        
        for i, visit in enumerate(recent, 1):
            evidence_summary = f"Evidence: {visit.evidence_score:.1f}"
            line = f"{i}. {visit.page_title} ({visit.domain}) - {evidence_summary}"
            lines.append(line)
            
            # Show top evidence entries
            if visit.evidence_vault:
                top_evidence = sorted(
                    visit.evidence_vault,
                    key=lambda e: e.get('goal_relevance_score', 0) * e.get('confidence', 0),
                    reverse=True
                )[:2]  # Top 2 evidence entries
                
                for ev in top_evidence:
                    excerpt = ev.get('excerpt', '')[:60]
                    if len(ev.get('excerpt', '')) > 60:
                        excerpt += "..."
                    lines.append(f"   - Found: {excerpt}")
        
        # Highlight current
        current = self.get_current_visit()
        if current:
            lines.append(f"\nCurrent: {current.page_title} ({current.domain})")
            if current.evidence_vault:
                lines.append(f"Evidence collected: {len(current.evidence_vault)} items")
        
        return "\n".join(lines)
    
    def record_visit_from_state(
        self,
        url: str,
        page_title: str,
        page_state: Any,
        site_map_node_id: Optional[str] = None,
    ) -> VisitRecord:
        """Create visit record from page state snapshot.
        
        This is a convenience method for creating visits when
        a full page state object is available.
        
        Args:
            url: Page URL
            page_title: Page title
            page_state: Page state object from page_state module
            site_map_node_id: Optional site map reference
            
        Returns:
            The newly created VisitRecord
        """
        return self.create_visit(
            url=url,
            page_title=page_title,
            page_node_id=site_map_node_id,
            page_state=page_state,
        )
    
    def get_cross_domain_context(self) -> Optional[str]:
        """Generate context when on different domain from parent.
        
        Returns a helpful message explaining the domain context
        when the user has navigated to a different domain from
        the mission target.
        
        Returns:
            Context message string, or None if same domain or no parent
            
        Example:
            >>> msg = graph.get_cross_domain_context()
            >>> print(msg)
            You are on a transitive/auth/support page (auth.example.com).
            Mission target remains on parent domain (console.groq.com).
        """
        current = self.get_current_visit()
        if not current:
            return None
        
        parent = self.get_parent_visit()
        if not parent:
            return None
        
        if current.domain == parent.domain:
            return None
        
        return (
            f"You are on a transitive/auth/support page ({current.domain}). "
            f"Mission target remains on parent domain ({parent.domain})."
        )
    
    def get_mission_summary(self) -> Dict[str, Any]:
        """Get summary statistics for the mission.
        
        Returns:
            Dictionary with mission visit statistics
        """
        highest = self.get_highest_evidence_visit()
        current = self.get_current_visit()
        
        if not self.visits:
            return {
                "mission_id": self.mission_id,
                "total_visits": 0,
                "total_evidence": 0,
                "total_evidence_score": 0.0,
                "domains_visited": [],
                "current_domain": None,
                "current_visit": None,
                "highest_evidence_visit": None,
            }
        
        total_evidence = sum(len(v.evidence_vault) for v in self.visits.values())
        total_score = sum(v.evidence_score for v in self.visits.values())
        domains = list(set(v.domain for v in self.visits.values()))
        
        return {
            "mission_id": self.mission_id,
            "total_visits": len(self.visits),
            "total_evidence": total_evidence,
            "total_evidence_score": round(total_score, 2),
            "domains_visited": domains,
            "current_domain": current.domain if current else None,
            "current_visit": self.current_visit_id,
            "highest_evidence_visit": highest.visit_id if highest else None,
            "root_visit_id": self.root_visit_id,
        }


def test_visit_graph():
    """Test visit graph functionality.
    
    This test function verifies:
    - Creating first visit (root)
    - Creating child visits (auto-linking)
    - Evidence vault accumulation
    - Highest evidence query
    - Backtrack suggestion
    - Breadcrumb generation
    - Cross-domain context
    """
    print("=" * 60)
    print("Testing Visit Graph v1")
    print("=" * 60)
    
    # Create visit graph
    graph = VisitGraphV1("test_mission_123")
    print(f"\n1. Created VisitGraph for mission: {graph.mission_id}")
    
    # Test 1: Create first visit (root)
    print("\n2. Creating first visit (root)...")
    visit1 = graph.create_visit(
        url="https://console.groq.com",
        page_title="Groq Console",
        page_node_id="node_1",
    )
    print(f"   - Visit ID: {visit1.visit_id}")
    print(f"   - Parent ID: {visit1.parent_visit_id} (should be None)")
    print(f"   - Root Visit ID: {graph.root_visit_id}")
    assert visit1.parent_visit_id is None, "Root should have no parent"
    assert graph.root_visit_id == visit1.visit_id, "Root should be set"
    print("   ✓ Root visit created successfully")
    
    # Test 2: Add evidence to first visit
    print("\n3. Adding evidence to first visit...")
    graph.add_evidence(
        excerpt="API Key: gsk_test123456789",
        source_label="API Keys Section",
        confidence=0.95,
        goal_relevance_score=0.9,
    )
    graph.add_evidence(
        excerpt="Model: llama-3.1-70b",
        source_label="Model Documentation",
        confidence=0.85,
        goal_relevance_score=0.7,
    )
    current = graph.get_current_visit()
    print(f"   - Evidence count: {len(current.evidence_vault)}")
    print(f"   - Evidence score: {current.evidence_score:.2f}")
    assert len(current.evidence_vault) == 2, "Should have 2 evidence entries"
    assert current.evidence_score > 0, "Should have positive score"
    print("   ✓ Evidence added successfully")
    
    # Test 3: Create child visit (auto-linking)
    print("\n4. Creating child visit (auto-linking)...")
    graph.mark_exit("click")  # Mark exit from first visit
    visit2 = graph.create_visit(
        url="https://console.groq.com/billing",
        page_title="Billing",
        page_node_id="node_billing",
    )
    print(f"   - Visit ID: {visit2.visit_id}")
    print(f"   - Parent ID: {visit2.parent_visit_id}")
    print(f"   - Current Visit ID: {graph.current_visit_id}")
    assert visit2.parent_visit_id == visit1.visit_id, "Should link to parent"
    assert graph.current_visit_id == visit2.visit_id, "Should be current"
    print("   ✓ Child visit created and linked")
    
    # Test 4: Create third visit
    print("\n5. Creating third visit...")
    graph.mark_exit("click")
    visit3 = graph.create_visit(
        url="https://docs.groq.com",
        page_title="Groq Documentation",
        page_node_id="node_docs",
    )
    print(f"   - Visit ID: {visit3.visit_id}")
    print(f"   - Domain: {visit3.domain}")
    print(f"   - Parent Domain: {graph.visits[visit3.parent_visit_id].domain}")
    assert visit3.domain != graph.visits[visit3.parent_visit_id].domain, "Cross-domain"
    print("   ✓ Cross-domain visit created")
    
    # Test 5: Get breadcrumb
    print("\n6. Testing breadcrumb generation...")
    breadcrumb = graph.get_breadcrumb()
    print(f"   - Breadcrumb length: {len(breadcrumb)}")
    for i, v in enumerate(breadcrumb, 1):
        print(f"   {i}. {v.page_title} ({v.domain})")
    assert len(breadcrumb) == 3, "Should have 3 visits in breadcrumb"
    assert breadcrumb[0].visit_id == visit1.visit_id, "First should be root"
    assert breadcrumb[-1].visit_id == visit3.visit_id, "Last should be current"
    print("   ✓ Breadcrumb correct")
    
    # Test 6: Get highest evidence visit
    print("\n7. Testing highest evidence query...")
    highest = graph.get_highest_evidence_visit()
    print(f"   - Highest evidence visit: {highest.page_title}")
    print(f"   - Evidence score: {highest.evidence_score:.2f}")
    assert highest.visit_id == visit1.visit_id, "First visit should have highest"
    print("   ✓ Highest evidence visit found")
    
    # Test 7: Test backtrack suggestion
    print("\n8. Testing backtrack suggestion...")
    backtrack = graph.suggest_backtrack()
    print(f"   - Current evidence: {visit3.evidence_score:.2f}")
    print(f"   - Suggested backtrack: {backtrack.page_title if backtrack else 'None'}")
    assert backtrack is not None, "Should suggest backtrack"
    assert backtrack.visit_id == visit1.visit_id, "Should suggest visit1"
    print("   ✓ Backtrack suggestion correct")
    
    # Test 8: Cross-domain context
    print("\n9. Testing cross-domain context...")
    context = graph.get_cross_domain_context()
    print(f"   - Context: {context}")
    assert "transitive" in context.lower() or "parent domain" in context.lower(), "Should mention domain context"
    print("   ✓ Cross-domain context generated")
    
    # Test 9: Context string
    print("\n10. Testing context string formatting...")
    context_str = graph.to_context_string()
    print(context_str)
    assert "Visit History" in context_str, "Should have header"
    assert "Groq Console" in context_str, "Should mention first visit"
    print("   ✓ Context string formatted")
    
    # Test 10: Mission summary
    print("\n11. Testing mission summary...")
    summary = graph.get_mission_summary()
    print(f"   - Total visits: {summary['total_visits']}")
    print(f"   - Total evidence: {summary['total_evidence']}")
    print(f"   - Domains: {summary['domains_visited']}")
    assert summary['total_visits'] == 3, "Should have 3 visits"
    assert summary['total_evidence'] == 2, "Should have 2 evidence entries"
    assert len(summary['domains_visited']) == 2, "Should have 2 domains"
    print("   ✓ Mission summary correct")
    
    # Test 11: Same domain query
    print("\n12. Testing same domain query...")
    same_domain = graph.get_latest_same_domain_visit("console.groq.com")
    print(f"   - Latest console.groq.com visit: {same_domain.page_title if same_domain else 'None'}")
    assert same_domain is not None, "Should find visit"
    assert same_domain.domain == "console.groq.com", "Should match domain"
    print("   ✓ Same domain query works")
    
    # Test 12: Should create new visit logic
    print("\n13. Testing should_create_new_visit logic...")
    # Same URL - should not create
    should_not_create = graph.should_create_new_visit("https://docs.groq.com")
    print(f"   - Same URL: {should_not_create} (should be False)")
    assert should_not_create is False, "Same URL should not create"
    
    # Different URL - should create
    should_create = graph.should_create_new_visit("https://docs.groq.com/quickstart")
    print(f"   - Different URL: {should_create} (should be True)")
    assert should_create is True, "Different URL should create"
    
    # Domain changed - should create
    should_create_domain = graph.should_create_new_visit(
        "https://other.com",
        domain_changed=True
    )
    print(f"   - Domain changed: {should_create_domain} (should be True)")
    assert should_create_domain is True, "Domain change should create"
    print("   ✓ New visit logic correct")
    
    print("\n" + "=" * 60)
    print("All tests passed! ✓")
    print("=" * 60)
    
    return graph


if __name__ == "__main__":
    test_visit_graph()

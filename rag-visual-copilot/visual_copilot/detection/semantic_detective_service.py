from typing import Any, List, Optional, Dict, Literal
from dataclasses import dataclass, field

from visual_copilot.constants import DETECTIVE_MIN_SCORE
from visual_copilot.routing.action_guard import _action_tag_compatible


@dataclass
class DetectiveReport:
    """
    Detective report with scored candidates and page analysis.
    Compatible with both legacy and new interfaces.
    """
    candidates: List[Any] = field(default_factory=list)
    best_match: Optional[Any] = None
    is_ambiguous: bool = False
    ambiguous_count: int = 0
    has_obstacle: bool = False
    obstacle_type: Optional[str] = None
    dismiss_button_id: Optional[str] = None
    page_type: str = "unknown"
    recommended_action: str = "click"
    confidence: Literal["high", "medium", "low"] = "medium"

    # Legacy attributes for visual_orchestrator compatibility
    complexity: str = "simple"
    recommended_model: str = "8b"
    routing_reason: str = "Legacy detective report"
    obstacle_dismiss_id: Optional[str] = None
    evidence: str = ""
    
    # Additional legacy attributes
    page_identity: str = ""
    form_fields: List[Dict[str, Any]] = field(default_factory=list)
    next_empty_field: Optional[Dict[str, Any]] = None


def _create_empty_report() -> DetectiveReport:
    """Create an empty detective report for fallback scenarios."""
    return DetectiveReport(
        candidates=[],
        best_match=None,
        confidence="low",
        recommended_action="wait",
        complexity="empty",
        recommended_model="8b",
        routing_reason="No candidates found",
    )


def _create_report_from_candidates(
    candidates: List[Any],
    best_match: Optional[Any],
    has_obstacle: bool = False,
    obstacle_type: Optional[str] = None,
    dismiss_button_id: Optional[str] = None,
    page_type: str = "unknown",
) -> DetectiveReport:
    """Create a detective report from candidates."""
    if not candidates or best_match is None:
        return _create_empty_report()
    
    # Determine complexity based on number of candidates and scores
    score = 0.0
    if best_match:
        score = float(getattr(best_match, "hybrid_score", 0.0) or getattr(best_match, "final_score", 0.0) or 0.0)
    
    if len(candidates) > 5:
        complexity = "complex"
    elif len(candidates) > 1:
        complexity = "moderate"
    else:
        complexity = "simple"
    
    # Determine recommended model based on complexity
    if complexity == "complex":
        recommended_model = "120b"
    elif complexity == "moderate":
        recommended_model = "8b"
    else:
        recommended_model = "shortcut" if score >= 0.8 else "8b"
    
    # Determine recommended action
    if has_obstacle and dismiss_button_id:
        recommended_action = "dismiss"
    elif page_type in ("form", "search"):
        recommended_action = "type"
    else:
        recommended_action = "click"
    
    # Determine confidence
    if score >= 0.7:
        confidence = "high"
    elif score >= 0.5:
        confidence = "medium"
    else:
        confidence = "low"
    
    # Build routing reason
    routing_reason = f"Found {len(candidates)} candidates, best match: '{getattr(best_match, 'text', 'unknown')[:50]}'"
    
    report = DetectiveReport(
        candidates=candidates,
        best_match=best_match,
        has_obstacle=has_obstacle,
        obstacle_type=obstacle_type,
        dismiss_button_id=dismiss_button_id,
        page_type=page_type,
        recommended_action=recommended_action,
        confidence=confidence,
        complexity=complexity,
        recommended_model=recommended_model,
        routing_reason=routing_reason,
        obstacle_dismiss_id=dismiss_button_id,
    )

    return report


async def investigate(
    *,
    goal: str,
    subgoal: str,
    dom_elements: List[Dict[str, Any]],
    action_history: Optional[List[Dict[str, Any]]] = None,
    reflexion_entries: Optional[List[Any]] = None,
    url: str = "",
    active_states: Optional[Dict[str, Any]] = None,
    data_tables: Optional[List[Dict[str, Any]]] = None,
    page_title: str = "",
    stagnation_action: str = "continue",
    requires_reasoning: bool = False,
) -> DetectiveReport:
    """
    Legacy investigate function for visual_orchestrator compatibility.
    
    This function adapts the legacy interface (with goal, dom_elements, etc.)
    to the new SemanticDetectiveService interface.
    
    Args:
        goal: User's main goal
        subgoal: Current subgoal description
        dom_elements: List of DOM elements to analyze
        action_history: Previous actions taken
        reflexion_entries: Reflection memory entries
        url: Current page URL
        active_states: Active UI states
        data_tables: Data table elements
        page_title: Page title
        stagnation_action: Action to take on stagnation
        requires_reasoning: Whether reasoning is required
        
    Returns:
        DetectiveReport with scored candidates and page analysis
    """
    # Import here to avoid circular dependencies
    from visual_copilot.semantic_detective import SemanticDetective
    from visual_copilot.live_graph import LiveGraph
    
    # Get the app state to access the semantic detective
    # This function is called from visual_orchestrator which has access to app.state
    import sys
    app_state = None
    
    # Try to get app_state from the call stack
    for frame_info in reversed(sys._current_frames().values()):
        if frame_info.f_locals.get('app') and hasattr(frame_info.f_locals['app'], 'state'):
            app_state = frame_info.f_locals['app'].state
            break
    
    # If we can't get app_state, return an empty report
    if not app_state or not hasattr(app_state, 'semantic_detective'):
        return _create_empty_report()
    
    semantic_detective = app_state.semantic_detective
    live_graph = app_state.live_graph
    
    if not semantic_detective or not live_graph:
        return _create_empty_report()
    
    try:
        # Extract session_id from action history or generate one
        session_id = ""
        for entry in reversed(action_history or []):
            if entry and entry.get("session_id"):
                session_id = entry["session_id"]
                break
        
        if not session_id:
            # Generate a session ID from the URL or goal
            session_id = f"legacy-{hash(url or goal) % 100000}"
        
        # Get visible nodes from live_graph
        nodes = await live_graph.get_visible_nodes(session_id)
        
        if not nodes:
            return _create_empty_report()
        
        # Filter to interactive nodes
        interactive_nodes = [n for n in nodes if getattr(n, "interactive", False)]
        
        if not interactive_nodes:
            return _create_empty_report()
        
        # Build candidates from nodes
        candidates = []
        for node in interactive_nodes:
            candidate = type('ScoredCandidate', (), {
                'node_id': getattr(node, 'id', ''),
                'text': getattr(node, 'text', ''),
                'tag': getattr(node, 'tag', ''),
                'zone': getattr(node, 'zone', ''),
                'hybrid_score': 0.5,  # Default score
                'final_score': 0.5,
            })()
            candidates.append(candidate)
        
        # Sort by score (descending)
        candidates.sort(key=lambda c: getattr(c, 'hybrid_score', 0.0), reverse=True)
        
        best_match = candidates[0] if candidates else None
        
        # Detect obstacles
        has_obstacle = False
        obstacle_type = None
        dismiss_button_id = None
        
        # Check for modal/cookie banner in DOM elements
        for elem in dom_elements or []:
            elem_text = (elem.get("text") or "").lower()
            elem_id = elem.get("id") or ""
            if any(kw in elem_text for kw in ["accept", "agree", "continue", "dismiss", "close", "x"]):
                if any(kw in elem_id for kw in ["modal", "cookie", "banner", "overlay", "popup"]):
                    has_obstacle = True
                    dismiss_button_id = elem.get("id")
                    obstacle_type = "cookie_banner" if "cookie" in elem_id else "modal"
                    break
        
        # Determine page type
        page_type = "unknown"
        page_title_lower = (page_title or "").lower()
        url_lower = (url or "").lower()
        
        if "form" in page_title_lower or "input" in str(dom_elements or []):
            page_type = "form"
        elif "search" in page_title_lower or "search" in url_lower:
            page_type = "search"
        elif "data" in page_title_lower or "table" in str(dom_elements or []):
            page_type = "data"
        elif "nav" in page_title_lower or "menu" in page_title_lower:
            page_type = "nav"
        
        # Create report from candidates
        report = _create_report_from_candidates(
            candidates=candidates,
            best_match=best_match,
            has_obstacle=has_obstacle,
            obstacle_type=obstacle_type,
            dismiss_button_id=dismiss_button_id,
            page_type=page_type,
        )
        
        # Add evidence for answer actions
        if best_match:
            report.evidence = getattr(best_match, "text", "")
        
        return report
        
    except Exception as e:
        logger = None
        try:
            from visual_copilot.logging.config import get_logger
            logger = get_logger("vc.orchestration.detective")
            logger.error(f"Detective investigation failed: {e}")
        except Exception:
            pass
        return _create_empty_report()


class SemanticDetectiveService:
    def __init__(self, detective: Any):
        self._detective = detective

    async def investigate(
        self,
        *,
        session_id: str,
        query: str,
        hive_hints: Optional[List[Any]],
        excluded_ids: Optional[List[str]],
        subgoal_mode: str,
    ) -> Any:
        return await self._detective.investigate(
            session_id=session_id,
            query=query,
            hive_hints=hive_hints or [],
            excluded_ids=excluded_ids or [],
            subgoal_mode=subgoal_mode,
        )

    @staticmethod
    def acceptable(report: Any, *, subgoal_mode: str, min_score: float = DETECTIVE_MIN_SCORE) -> bool:
        best = getattr(report, "best_match", None)
        if not best:
            return False
        score = float(getattr(best, "final_score", 0.0) or getattr(best, "hybrid_score", 0.0) or 0.0)
        if score < min_score:
            return False
        return _action_tag_compatible(subgoal_mode, best)

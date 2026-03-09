"""
Page Locator - Runtime page resolution for TARA Visual Copilot.

This module provides runtime page resolution when no static site map exists.
It synthesizes dynamic nodes from the current page structure, enabling the
agent to navigate even unmapped domains.

Features:
- Synthesize dynamic nodes when no static map exists
- Normalize into PageNodeRef structure
- Extract controls from live DOM
- Fallback to URL-based heuristics
"""

import logging
import re
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from urllib.parse import urlparse
import hashlib

logger = logging.getLogger(__name__)


@dataclass
class PageNodeRef:
    """
    Reference to a page node (unified structure for static and dynamic nodes).
    
    This structure is compatible with the RecoveryState.page_node field.
    
    Attributes:
        node_id: Unique identifier for the node
        logical_path: Hierarchical path (e.g., "root.console.playground")
        source: Origin of the node ("static_map" or "dynamic_runtime")
        url_pattern: Regex pattern for matching URLs
        current_url: Current browser URL
        expected_controls: List of expected UI controls on this page
        parent_node_id: Parent node ID in hierarchy
        title: Page title
        summary: Brief description of page contents
        capabilities: List of possible actions from this page
    """
    node_id: str
    logical_path: str
    source: str = "dynamic_runtime"  # "static_map" | "dynamic_runtime"
    url_pattern: str = ""
    current_url: str = ""
    expected_controls: List[str] = field(default_factory=list)
    parent_node_id: str = ""
    title: str = ""
    summary: str = ""
    capabilities: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "node_id": self.node_id,
            "logical_path": self.logical_path,
            "source": self.source,
            "url_pattern": self.url_pattern,
            "current_url": self.current_url,
            "expected_controls": self.expected_controls,
            "parent_node_id": self.parent_node_id,
            "title": self.title,
            "summary": self.summary,
            "capabilities": self.capabilities
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PageNodeRef":
        """Create from dictionary"""
        return cls(
            node_id=data.get("node_id", ""),
            logical_path=data.get("logical_path", ""),
            source=data.get("source", "dynamic_runtime"),
            url_pattern=data.get("url_pattern", ""),
            current_url=data.get("current_url", ""),
            expected_controls=data.get("expected_controls", []),
            parent_node_id=data.get("parent_node_id", ""),
            title=data.get("title", ""),
            summary=data.get("summary", ""),
            capabilities=data.get("capabilities", [])
        )
    
    @staticmethod
    def generate_node_id(url: str, title: str = "") -> str:
        """Generate a unique node ID from URL and title"""
        # Create hash from URL path and title
        content = f"{url}:{title}"
        hash_digest = hashlib.md5(content.encode()).hexdigest()[:8]
        
        # Extract path segments
        try:
            parsed = urlparse(url)
            path_segments = [s for s in parsed.path.split("/") if s]
            path_prefix = "_".join(path_segments[:3]) if path_segments else "page"
        except Exception:
            path_prefix = "page"
        
        return f"{path_prefix}_{hash_digest}"


class PageLocator:
    """
    Runtime page resolution service.
    
    Synthesizes page nodes when no static site map exists.
    Uses URL patterns, DOM analysis, and heuristics to understand page structure.
    
    Usage:
        locator = PageLocator()
        node = await locator.resolve_page_node(
            url="https://example.com/dashboard",
            title="Dashboard",
            dom_elements=[...]
        )
    """
    
    # Common URL patterns for dynamic node synthesis
    URL_PATTERNS = {
        r"^/$": "home",
        r"^/home/?$": "home",
        r"^/dashboard/?$": "dashboard",
        r"^/settings/?$": "settings",
        r"^/profile/?$": "profile",
        r"^/account/?$": "account",
        r"^/login/?$": "login",
        r"^/logout/?$": "logout",
        r"^/signup/?$": "signup",
        r"^/docs/?$": "docs",
        r"^/api/?$": "api",
        r"^/playground/?$": "playground",
        r"^/console/?$": "console",
    }
    
    # Common control patterns
    CONTROL_PATTERNS = {
        r"button": "button",
        r"input": "input_field",
        r"select": "dropdown",
        r"checkbox": "checkbox",
        r"radio": "radio_button",
        r"link": "link",
        r"nav": "navigation",
        r"menu": "menu",
        r"table": "data_table",
        r"form": "form",
        r"search": "search_box",
        r"filter": "filter",
        r"sort": "sort_control",
    }
    
    def __init__(self):
        """Initialize PageLocator"""
        self.synthesized_nodes: Dict[str, PageNodeRef] = {}
        logger.info("PageLocator initialized")
    
    async def resolve_page_node(
        self,
        url: str,
        title: str = "",
        dom_elements: Optional[List[Dict[str, Any]]] = None,
        static_node: Optional[Any] = None
    ) -> PageNodeRef:
        """
        Resolve the current page node.
        
        If a static node is provided, uses it. Otherwise, synthesizes a dynamic
        node from the URL and DOM.
        
        Args:
            url: Current page URL
            title: Page title
            dom_elements: Optional list of DOM elements for analysis
            static_node: Optional static SiteNode from PageIndex
            
        Returns:
            PageNodeRef for the current page
        """
        # Use static node if available
        if static_node:
            return PageNodeRef(
                node_id=static_node.node_id,
                logical_path=static_node.logical_path,
                source="static_map",
                url_pattern=static_node.path_regex,
                current_url=url,
                expected_controls=static_node.expected_controls,
                parent_node_id=static_node.parent_node_id,
                title=static_node.title,
                summary=static_node.summary_of_contents,
                capabilities=static_node.terminal_capabilities
            )
        
        # Synthesize dynamic node
        return await self.synthesize_dynamic_node(url, title, dom_elements)
    
    async def synthesize_dynamic_node(
        self,
        url: str,
        title: str = "",
        dom_elements: Optional[List[Dict[str, Any]]] = None
    ) -> PageNodeRef:
        """
        Synthesize a dynamic node from URL and DOM.
        
        Creates a PageNodeRef when no static site map exists.
        
        Args:
            url: Current page URL
            title: Page title
            dom_elements: Optional list of DOM elements for analysis
            
        Returns:
            Synthesized PageNodeRef
        """
        try:
            # Parse URL
            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.", "")
            path = parsed.path
            
            # Generate node ID
            node_id = PageNodeRef.generate_node_id(url, title)
            
            # Determine logical path from URL
            logical_path = self._extract_logical_path(path, title)
            
            # Generate URL pattern
            url_pattern = self._generate_url_pattern(path)
            
            # Extract expected controls from DOM
            expected_controls = []
            capabilities = []
            
            if dom_elements:
                expected_controls = self._extract_controls_from_dom(dom_elements)
                capabilities = self._infer_capabilities(dom_elements, path)
            
            # Generate summary
            summary = self._generate_summary(title, path, expected_controls)
            
            # Create node reference
            node = PageNodeRef(
                node_id=node_id,
                logical_path=logical_path,
                source="dynamic_runtime",
                url_pattern=url_pattern,
                current_url=url,
                expected_controls=expected_controls,
                parent_node_id="root",  # Default to root for dynamic nodes
                title=title,
                summary=summary,
                capabilities=capabilities
            )
            
            # Cache synthesized node
            self.synthesized_nodes[node_id] = node
            
            logger.info(
                f"🔧 Dynamic node synthesized | url={url} | "
                f"node={node_id} | path={logical_path} | "
                f"controls={len(expected_controls)}"
            )
            
            return node
            
        except Exception as e:
            logger.error(f"Failed to synthesize dynamic node: {e}", exc_info=True)
            
            # Fallback to minimal node
            return self._create_fallback_node(url, title)
    
    def _extract_logical_path(self, path: str, title: str) -> str:
        """Extract logical path from URL path"""
        # Clean path
        path = path.strip("/")
        
        if not path:
            return "root.home"
        
        # Split into segments
        segments = path.split("/")
        
        # Build logical path
        logical_parts = ["root"]
        for segment in segments:
            # Clean segment (remove IDs, parameters)
            clean_segment = re.sub(r'\d+', '', segment)
            clean_segment = clean_segment.replace("-", "_").replace(".", "")
            
            if clean_segment:
                logical_parts.append(clean_segment)
        
        # Append title-based hint if available
        if title and len(logical_parts) < 4:
            title_hint = title.lower().split()[0] if title else ""
            title_hint = re.sub(r'[^a-z]', '', title_hint)
            if title_hint and title_hint not in logical_parts[-1]:
                logical_parts.append(title_hint)
        
        return ".".join(logical_parts)
    
    def _generate_url_pattern(self, path: str) -> str:
        """Generate regex pattern for URL matching"""
        # Escape special regex characters
        pattern = re.escape(path)
        
        # Replace numeric segments with wildcard
        pattern = re.sub(r'\\d+', r'\\d+', pattern)
        
        # Add optional trailing slash
        if not pattern.endswith("/"):
            pattern += "/?"
        
        # Anchor pattern
        return f"^{pattern}$"
    
    def _extract_controls_from_dom(
        self,
        dom_elements: List[Dict[str, Any]]
    ) -> List[str]:
        """Extract expected controls from DOM elements"""
        controls = set()
        
        for element in dom_elements:
            tag = element.get("tagName", "").lower()
            role = element.get("role", "").lower()
            aria_role = element.get("aria-role", "").lower()
            class_name = element.get("className", "").lower()
            id_attr = element.get("id", "").lower()
            
            # Check tag name
            for pattern, control_type in self.CONTROL_PATTERNS.items():
                if re.search(pattern, tag):
                    controls.add(control_type)
            
            # Check role
            if role in ["button", "link", "textbox", "combobox"]:
                controls.add(role)
            if aria_role in ["button", "link", "textbox", "combobox"]:
                controls.add(aria_role)
            
            # Check class/id hints
            for pattern, control_type in self.CONTROL_PATTERNS.items():
                if pattern in class_name or pattern in id_attr:
                    controls.add(control_type)
        
        return sorted(list(controls))
    
    def _infer_capabilities(
        self,
        dom_elements: List[Dict[str, Any]],
        path: str
    ) -> List[str]:
        """Infer page capabilities from DOM and URL"""
        capabilities = set()
        
        # URL-based inferences
        if "/playground" in path:
            capabilities.add("run_model_inference")
            capabilities.add("test_prompts")
        if "/dashboard" in path or "/usage" in path:
            capabilities.add("read_metrics")
            capabilities.add("view_usage")
        if "/keys" in path or "/api-keys" in path:
            capabilities.add("manage_api_keys")
            capabilities.add("create_key")
        if "/docs" in path:
            capabilities.add("read_documentation")
        if "/settings" in path or "/account" in path:
            capabilities.add("update_settings")
            capabilities.add("manage_account")
        if "/batch" in path:
            capabilities.add("manage_batch_jobs")
        if "/logs" in path:
            capabilities.add("view_logs")
        
        # DOM-based inferences
        for element in dom_elements:
            tag = element.get("tagName", "").lower()
            text = element.get("textContent", "").lower()
            
            # Look for specific action indicators
            if tag == "button" or element.get("role") == "button":
                if "submit" in text or "send" in text:
                    capabilities.add("submit_form")
                if "create" in text or "new" in text:
                    capabilities.add("create_resource")
                if "delete" in text or "remove" in text:
                    capabilities.add("delete_resource")
                if "save" in text:
                    capabilities.add("save_changes")
        
        return sorted(list(capabilities))
    
    def _generate_summary(
        self,
        title: str,
        path: str,
        controls: List[str]
    ) -> str:
        """Generate a summary of the page"""
        parts = []
        
        # Add title
        if title:
            parts.append(f"Page: {title}")
        
        # Add path context
        if path:
            parts.append(f"Path: {path}")
        
        # Add controls summary
        if controls:
            parts.append(f"Controls: {', '.join(controls[:5])}")
        
        return ". ".join(parts) if parts else "Dynamic page node"
    
    def _create_fallback_node(self, url: str, title: str) -> PageNodeRef:
        """Create a minimal fallback node when synthesis fails"""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.", "")
            path = parsed.path or "/"
        except Exception:
            domain = "unknown"
            path = "/"
        
        return PageNodeRef(
            node_id=f"fallback_{hashlib.md5(url.encode()).hexdigest()[:8]}",
            logical_path=f"root.{domain.replace('.', '_')}.{path.strip('/').replace('/', '_') or 'home'}",
            source="dynamic_runtime",
            url_pattern=".*",
            current_url=url,
            expected_controls=[],
            parent_node_id="root",
            title=title,
            summary=f"Fallback node for {url}",
            capabilities=[]
        )
    
    def get_synthesized_node(self, node_id: str) -> Optional[PageNodeRef]:
        """Get a previously synthesized node by ID"""
        return self.synthesized_nodes.get(node_id)
    
    def clear_synthesized_nodes(self) -> None:
        """Clear all synthesized nodes"""
        self.synthesized_nodes.clear()
        logger.info("Cleared synthesized nodes")


# Singleton instance
_page_locator_instance: Optional[PageLocator] = None


def get_page_locator() -> PageLocator:
    """Get or create the singleton PageLocator instance"""
    global _page_locator_instance
    if _page_locator_instance is None:
        _page_locator_instance = PageLocator()
    return _page_locator_instance


# Convenience function
async def resolve_page(
    url: str,
    title: str = "",
    dom_elements: Optional[List[Dict[str, Any]]] = None
) -> PageNodeRef:
    """Resolve page node (uses static map if available, otherwise synthesizes)"""
    from .page_index import get_page_index
    
    # Try static map first
    page_index = get_page_index()
    static_node = page_index.resolve_current_node(url)
    
    # Use locator for resolution
    locator = get_page_locator()
    return await locator.resolve_page_node(url, title, dom_elements, static_node)

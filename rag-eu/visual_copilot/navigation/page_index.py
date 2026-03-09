"""
PageIndex Service - Main PageIndex service for TARA Visual Copilot.

This module provides hierarchical navigation intelligence by loading and querying
static site maps (site_map.json). It enables the agent to understand its position
within a web application and navigate efficiently without recursive exploration.

Features:
- Load static maps from site_map.json files
- Index by domain and regex path
- Resolve current node from URL
- Get child nodes for navigation planning
- Prevent navigation loops through explicit visited-node tracking
"""

import json
import logging
import re
import os
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass
class SiteNode:
    """
    Represents a node in the site map hierarchy.
    
    Attributes:
        node_id: Unique identifier for the node
        title: Human-readable title
        logical_path: Hierarchical path (e.g., "root.console.playground")
        url: Canonical URL for this node
        path_regex: Regex pattern for matching URLs to this node
        summary_of_contents: Description of page contents
        expected_controls: List of expected UI controls
        required_controls: List of controls that must be present
        terminal_capabilities: List of terminal actions possible from this node
        children: List of child node IDs
        parent_node_id: Parent node ID in hierarchy
    """
    node_id: str
    title: str
    logical_path: str
    url: str = ""
    path_regex: str = ""
    summary_of_contents: str = ""
    expected_controls: List[str] = field(default_factory=list)
    required_controls: List[str] = field(default_factory=list)
    terminal_capabilities: List[str] = field(default_factory=list)
    children: List[str] = field(default_factory=list)
    parent_node_id: str = ""
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any], parent_id: str = "") -> "SiteNode":
        """Create SiteNode from dictionary (site_map.json format)"""
        return cls(
            node_id=data.get("node_id", ""),
            title=data.get("title", ""),
            logical_path=data.get("logical_path", ""),
            url=data.get("url", ""),
            path_regex=data.get("path_regex", ""),
            summary_of_contents=data.get("summary_of_contents", ""),
            expected_controls=data.get("expected_controls", []),
            required_controls=data.get("required_controls", []),
            terminal_capabilities=data.get("terminal_capabilities", []),
            children=[c.get("node_id") for c in data.get("children", [])],
            parent_node_id=parent_id
        )
    
    def matches_url(self, url: str) -> bool:
        """Check if a URL matches this node's path_regex"""
        if not self.path_regex:
            return False
        
        try:
            # Extract path from URL
            parsed = urlparse(url)
            path = parsed.path
            
            # Match against regex
            return bool(re.match(self.path_regex, path))
        except Exception:
            return False
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "node_id": self.node_id,
            "title": self.title,
            "logical_path": self.logical_path,
            "url": self.url,
            "path_regex": self.path_regex,
            "summary_of_contents": self.summary_of_contents,
            "expected_controls": self.expected_controls,
            "required_controls": self.required_controls,
            "terminal_capabilities": self.terminal_capabilities,
            "children": self.children,
            "parent_node_id": self.parent_node_id
        }


@dataclass
class SiteMap:
    """
    Complete site map for a domain.
    
    Attributes:
        domain: Domain this map covers (e.g., "console.groq.com")
        version: Map version
        root: Root node of the hierarchy
        nodes: Flat dictionary of all nodes by ID
        path_regex_index: Index of path_regex patterns for fast lookup
    """
    domain: str
    version: str = "1.0.0"
    root: Optional[SiteNode] = None
    nodes: Dict[str, SiteNode] = field(default_factory=dict)
    path_regex_index: List[Tuple[str, str]] = field(default_factory=list)  # (regex, node_id)
    
    def add_node(self, node: SiteNode) -> None:
        """Add a node to the map"""
        self.nodes[node.node_id] = node
        if node.path_regex:
            self.path_regex_index.append((node.path_regex, node.node_id))
    
    def get_node(self, node_id: str) -> Optional[SiteNode]:
        """Get a node by ID"""
        return self.nodes.get(node_id)
    
    def get_child_nodes(self, parent_node_id: str) -> List[SiteNode]:
        """Get all child nodes of a parent"""
        parent = self.nodes.get(parent_node_id)
        if not parent:
            return []
        
        return [
            self.nodes[child_id]
            for child_id in parent.children
            if child_id in self.nodes
        ]
    
    def find_node_by_path_regex(self, url: str) -> Optional[SiteNode]:
        """Find the best matching node for a URL using path_regex"""
        best_match = None
        best_specificity = 0
        
        for regex, node_id in self.path_regex_index:
            try:
                parsed = urlparse(url)
                path = parsed.path
                
                match = re.match(regex, path)
                if match:
                    # Prefer more specific matches (longer regex, more groups)
                    specificity = len(regex) + len(match.groups())
                    if specificity > best_specificity:
                        best_specificity = specificity
                        best_match = self.nodes.get(node_id)
            except Exception:
                continue
        
        return best_match


class PageIndex:
    """
    Main PageIndex service for site map management.
    
    Loads static maps from site_map.json files and provides methods to:
    - Resolve current node from URL
    - Get child nodes for navigation
    - Check terminal capabilities
    - Prevent navigation loops
    
    Usage:
        index = PageIndex()
        await index.load_domain_map("console.groq.com", "/path/to/site_map.json")
        node = index.resolve_current_node("https://console.groq.com/playground")
        children = index.get_child_nodes(node.node_id)
    """
    
    # Default site map file locations
    DEFAULT_SITE_MAP_PATHS = [
        "/Users/amar/demo.davinciai/rag-visual-copilot/site_map.json",
        "./rag-visual-copilot/site_map.json",
        "../rag-visual-copilot/site_map.json",
    ]
    
    def __init__(self):
        """Initialize PageIndex service"""
        self.domain_maps: Dict[str, SiteMap] = {}
        self.loaded_paths: set = set()
        self._autoload_default_maps()
        logger.info("PageIndex service initialized")

    def _autoload_default_maps(self) -> None:
        file_path = self._find_site_map_file()
        if not file_path or not os.path.exists(file_path):
            return
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            metadata = data.get("site_metadata", {})
            domain = metadata.get("domain", "")
            if not domain:
                return
            site_map = SiteMap(domain=domain, version=metadata.get("version", "1.0.0"))
            if "root" in data:
                root_node = self._parse_node_recursive(data["root"], parent_id="")
                site_map.root = root_node
                self._index_nodes_recursive(site_map, data["root"])
            self.domain_maps[domain] = site_map
            self.loaded_paths.add(file_path)
        except Exception as e:
            logger.debug(f"Failed to autoload default site map: {e}")
    
    async def load_domain_map(
        self,
        domain: str,
        file_path: Optional[str] = None
    ) -> Optional[SiteMap]:
        """
        Load a site map for a specific domain.
        
        Args:
            domain: Domain to load map for (e.g., "console.groq.com")
            file_path: Optional path to site_map.json. If None, searches default paths.
            
        Returns:
            Loaded SiteMap, or None if failed
        """
        # Check if already loaded
        if domain in self.domain_maps:
            logger.debug(f"Site map already loaded for domain={domain}")
            return self.domain_maps[domain]
        
        # Determine file path
        if file_path is None:
            file_path = self._find_site_map_file()
        
        if not file_path or not os.path.exists(file_path):
            logger.warning(f"Site map file not found for domain={domain}")
            return None
        
        try:
            logger.info(f"📖 Loading site map from {file_path} for domain={domain}")
            
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Parse site metadata
            metadata = data.get("site_metadata", {})
            map_domain = metadata.get("domain", domain)
            version = metadata.get("version", "1.0.0")
            
            # Create site map
            site_map = SiteMap(domain=map_domain, version=version)
            
            # Parse root node recursively
            if "root" in data:
                root_node = self._parse_node_recursive(data["root"], parent_id="")
                site_map.root = root_node
                self._index_nodes_recursive(site_map, data["root"])
            
            # Store in registry
            self.domain_maps[domain] = site_map
            self.loaded_paths.add(file_path)
            
            logger.info(
                f"✅ Site map loaded | domain={domain} | "
                f"nodes={len(site_map.nodes)} | version={version}"
            )
            
            return site_map
            
        except Exception as e:
            logger.error(f"Failed to load site map: {e}", exc_info=True)
            return None
    
    def _find_site_map_file(self) -> Optional[str]:
        """Find site_map.json in default locations"""
        for path in self.DEFAULT_SITE_MAP_PATHS:
            if os.path.exists(path):
                return path
        return None
    
    def _parse_node_recursive(
        self,
        data: Dict[str, Any],
        parent_id: str = ""
    ) -> SiteNode:
        """Parse a node and its children recursively"""
        node = SiteNode.from_dict(data, parent_id)
        return node
    
    def _index_nodes_recursive(
        self,
        site_map: SiteMap,
        data: Dict[str, Any],
        parent_id: str = ""
    ) -> None:
        """Recursively index all nodes in the hierarchy"""
        node = SiteNode.from_dict(data, parent_id)
        site_map.add_node(node)
        
        # Process children
        for child_data in data.get("children", []):
            self._index_nodes_recursive(site_map, child_data, node.node_id)
    
    def resolve_current_node(
        self,
        url: str,
        domain: Optional[str] = None
    ) -> Optional[SiteNode]:
        """
        Resolve the current page node from a URL.
        
        Searches loaded site maps to find the best matching node.
        
        Args:
            url: Current page URL
            domain: Optional domain hint. If None, extracted from URL.
            
        Returns:
            Matching SiteNode, or None if not found
        """
        # Extract domain from URL if not provided
        if domain is None:
            try:
                parsed = urlparse(url)
                domain = parsed.netloc.replace("www.", "")
            except Exception:
                logger.warning(f"Failed to extract domain from URL: {url}")
                return None
        
        # Get site map for domain
        site_map = self.domain_maps.get(domain)
        if not site_map:
            logger.debug(f"No site map loaded for domain={domain}")
            return None
        
        # Find matching node
        node = site_map.find_node_by_path_regex(url)
        
        if node:
            logger.debug(
                f"📍 Node resolved | url={url} | "
                f"node={node.node_id} | path={node.logical_path}"
            )
        else:
            logger.debug(f"No matching node found for url={url}")
        
        return node
    
    def get_child_nodes(
        self,
        node_id: str,
        domain: Optional[str] = None
    ) -> List[SiteNode]:
        """
        Get child nodes for navigation planning.
        
        Args:
            node_id: Parent node ID
            domain: Optional domain hint
            
        Returns:
            List of child SiteNodes
        """
        # If domain not specified, search all loaded maps
        if domain is None:
            for site_map in self.domain_maps.values():
                if node_id in site_map.nodes:
                    return site_map.get_child_nodes(node_id)
            return []
        
        # Get from specific domain map
        site_map = self.domain_maps.get(domain)
        if not site_map:
            return []
        
        return site_map.get_child_nodes(node_id)
    
    def get_node(
        self,
        node_id: str,
        domain: Optional[str] = None
    ) -> Optional[SiteNode]:
        """
        Get a specific node by ID.
        
        Args:
            node_id: Node identifier
            domain: Optional domain hint
            
        Returns:
            SiteNode if found, None otherwise
        """
        # If domain not specified, search all loaded maps
        if domain is None:
            for site_map in self.domain_maps.values():
                if node_id in site_map.nodes:
                    return site_map.nodes[node_id]
            return None
        
        # Get from specific domain map
        site_map = self.domain_maps.get(domain)
        if not site_map:
            return None
        
        return site_map.get_node(node_id)
    
    def get_terminal_capabilities(
        self,
        node_id: str,
        domain: Optional[str] = None
    ) -> List[str]:
        """
        Get terminal capabilities for a node.
        
        Terminal capabilities indicate actions that can complete a mission
        from this node (e.g., "run_model_inference", "create_api_key").
        
        Args:
            node_id: Node identifier
            domain: Optional domain hint
            
        Returns:
            List of terminal capability strings
        """
        node = self.get_node(node_id, domain)
        if not node:
            return []
        
        return node.terminal_capabilities
    
    def is_terminal_node(
        self,
        node_id: str,
        domain: Optional[str] = None
    ) -> bool:
        """
        Check if a node is terminal (has terminal capabilities).
        
        Terminal nodes are leaf nodes where missions typically complete.
        
        Args:
            node_id: Node identifier
            domain: Optional domain hint
            
        Returns:
            True if node is terminal
        """
        capabilities = self.get_terminal_capabilities(node_id, domain)
        return len(capabilities) > 0
    
    def get_navigation_path(
        self,
        from_node_id: str,
        to_node_id: str,
        domain: Optional[str] = None
    ) -> List[str]:
        """
        Get the navigation path between two nodes.
        
        Args:
            from_node_id: Starting node ID
            to_node_id: Target node ID
            domain: Optional domain hint
            
        Returns:
            List of node IDs representing the path
        """
        # Get domain map
        if domain is None:
            for site_map in self.domain_maps.values():
                if from_node_id in site_map.nodes and to_node_id in site_map.nodes:
                    return self._find_path_in_map(site_map, from_node_id, to_node_id)
            return []
        
        site_map = self.domain_maps.get(domain)
        if not site_map:
            return []
        
        return self._find_path_in_map(site_map, from_node_id, to_node_id)
    
    def _find_path_in_map(
        self,
        site_map: SiteMap,
        from_id: str,
        to_id: str
    ) -> List[str]:
        """Find path between nodes in a site map using BFS"""
        if from_id == to_id:
            return [from_id]
        
        # Build parent map
        parent_map = {}
        for node_id, node in site_map.nodes.items():
            if node.parent_node_id:
                parent_map[node_id] = node.parent_node_id
        
        # Get ancestors of from_id
        from_ancestors = set()
        current = from_id
        while current:
            from_ancestors.add(current)
            current = parent_map.get(current)
        
        # BFS from to_id to find common ancestor
        queue = [(to_id, [to_id])]
        visited = {to_id}
        
        while queue:
            current, path = queue.pop(0)
            
            if current in from_ancestors:
                # Found common ancestor, build full path
                # Path: from_id -> ... -> ancestor -> ... -> to_id
                from_to_ancestor = []
                temp = from_id
                while temp != current:
                    from_to_ancestor.append(temp)
                    temp = parent_map.get(temp)
                from_to_ancestor.append(current)
                
                return from_to_ancestor + list(reversed(path[:-1]))
            
            # Add parent to queue
            parent = parent_map.get(current)
            if parent and parent not in visited:
                visited.add(parent)
                queue.append((parent, path + [parent]))
            
            # Add children to queue
            node = site_map.nodes.get(current)
            if node:
                for child_id in node.children:
                    if child_id not in visited:
                        visited.add(child_id)
                        queue.append((child_id, path + [child_id]))
        
        return []  # No path found
    
    def get_all_domains(self) -> List[str]:
        """Get list of all loaded domains"""
        return list(self.domain_maps.keys())
    
    def get_map_summary(self, domain: str) -> Optional[Dict[str, Any]]:
        """
        Get summary information about a site map.
        
        Args:
            domain: Domain to get summary for
            
        Returns:
            Dictionary with map statistics
        """
        site_map = self.domain_maps.get(domain)
        if not site_map:
            return None
        
        # Count nodes by depth
        depth_counts = {}
        for node in site_map.nodes.values():
            depth = node.logical_path.count(".")
            depth_counts[depth] = depth_counts.get(depth, 0) + 1
        
        return {
            "domain": site_map.domain,
            "version": site_map.version,
            "total_nodes": len(site_map.nodes),
            "depth_distribution": depth_counts,
            "root_node": site_map.root.node_id if site_map.root else None,
            "regex_patterns": len(site_map.path_regex_index)
        }


# Singleton instance
_page_index_instance: Optional[PageIndex] = None


def get_page_index() -> PageIndex:
    """Get or create the singleton PageIndex instance"""
    global _page_index_instance
    if _page_index_instance is None:
        _page_index_instance = PageIndex()
    return _page_index_instance


# Convenience functions
async def load_site_map(domain: str, file_path: Optional[str] = None) -> Optional[SiteMap]:
    """Load a site map for a domain"""
    index = get_page_index()
    return await index.load_domain_map(domain, file_path)


def resolve_node(url: str, domain: Optional[str] = None) -> Optional[SiteNode]:
    """Resolve current node from URL"""
    index = get_page_index()
    return index.resolve_current_node(url, domain)


def get_children(node_id: str, domain: Optional[str] = None) -> List[SiteNode]:
    """Get child nodes"""
    index = get_page_index()
    return index.get_child_nodes(node_id, domain)

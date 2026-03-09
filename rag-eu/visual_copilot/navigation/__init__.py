"""
Navigation Module - PageIndex-based navigation intelligence for TARA Visual Copilot.

This module provides hierarchical site map management and runtime page resolution,
enabling efficient navigation without recursive exploration.

Components:
- PageIndex: Load and query static site maps
- PageLocator: Runtime page resolution and dynamic node synthesis
- PageRegistry: Domain map registry and management

Usage:
    from visual_copilot.navigation import PageIndex, PageLocator, PageRegistry
    
    # Load site map
    index = PageIndex()
    await index.load_domain_map("console.groq.com")
    
    # Resolve current page
    node = index.resolve_current_node("https://console.groq.com/playground")
    
    # Get child nodes for navigation
    children = index.get_child_nodes(node.node_id)
"""

from .page_index import (
    PageIndex,
    SiteMap,
    SiteNode,
    get_page_index,
    load_site_map,
    resolve_node,
    get_children
)

from .page_locator import (
    PageLocator,
    PageNodeRef,
    get_page_locator,
    resolve_page
)

from .page_registry import (
    PageRegistry,
    DomainEntry,
    get_page_registry,
    register_domain,
    get_domain_map,
    resolve_domain_from_url
)

__all__ = [
    # PageIndex
    "PageIndex",
    "SiteMap",
    "SiteNode",
    "get_page_index",
    "load_site_map",
    "resolve_node",
    "get_children",
    
    # PageLocator
    "PageLocator",
    "PageNodeRef",
    "get_page_locator",
    "resolve_page",
    
    # PageRegistry
    "PageRegistry",
    "DomainEntry",
    "get_page_registry",
    "register_domain",
    "get_domain_map",
    "resolve_domain_from_url",
]

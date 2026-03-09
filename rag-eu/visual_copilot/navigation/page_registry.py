"""
Page Registry - Domain map registry for TARA Visual Copilot.

This module maintains a registry of loaded domain maps and provides
methods to register, retrieve, and manage site maps across domains.

Features:
- Registry of loaded domain maps
- Automatic domain detection from URLs
- Lazy loading of site maps
- Domain alias support
"""

import logging
from typing import Optional, Dict, Any, List, Set
from dataclasses import dataclass, field
from urllib.parse import urlparse
import time

logger = logging.getLogger(__name__)


@dataclass
class DomainEntry:
    """
    Registry entry for a domain.
    
    Attributes:
        domain: Primary domain name
        site_map: Associated SiteMap object
        aliases: List of domain aliases (e.g., www. prefix)
        loaded_at: Timestamp when map was loaded
        access_count: Number of times this map has been accessed
        auto_load: Whether to auto-load this map on first access
    """
    domain: str
    site_map: Optional[Any] = None  # SiteMap from page_index
    aliases: Set[str] = field(default_factory=set)
    loaded_at: float = field(default_factory=time.time)
    access_count: int = 0
    auto_load: bool = True
    
    def add_alias(self, alias: str) -> None:
        """Add a domain alias"""
        # Normalize alias (remove www. prefix for consistency)
        normalized = alias.replace("www.", "")
        self.aliases.add(normalized)
        self.aliases.add(alias)
    
    def is_alias(self, domain: str) -> bool:
        """Check if a domain is an alias of this entry"""
        normalized = domain.replace("www.", "")
        return normalized in self.aliases or normalized == self.domain


class PageRegistry:
    """
    Registry of loaded domain maps.
    
    Maintains a central registry of all loaded site maps, enabling
    efficient lookup and management across the Visual Copilot system.
    
    Usage:
        registry = PageRegistry()
        registry.register_domain("console.groq.com", site_map)
        entry = registry.get_domain_map("console.groq.com")
        entry = registry.get_domain_map("www.console.groq.com")  # Alias works
    """
    
    def __init__(self):
        """Initialize PageRegistry"""
        self.domains: Dict[str, DomainEntry] = {}
        self.aliases: Dict[str, str] = {}  # alias -> primary domain
        self._lock = False  # Simple lock for thread safety
        logger.info("PageRegistry initialized")
    
    def register_domain(
        self,
        domain: str,
        site_map: Optional[Any] = None,
        aliases: Optional[List[str]] = None,
        auto_load: bool = True
    ) -> DomainEntry:
        """
        Register a domain in the registry.
        
        Args:
            domain: Primary domain name
            site_map: Optional SiteMap object
            aliases: Optional list of domain aliases
            auto_load: Whether to auto-load on first access
            
        Returns:
            Created DomainEntry
        """
        # Normalize domain
        domain = domain.replace("www.", "").lower()
        
        # Check if already registered
        if domain in self.domains:
            entry = self.domains[domain]
            if site_map:
                entry.site_map = site_map
            if aliases:
                for alias in aliases:
                    entry.add_alias(alias)
            logger.debug(f"Updated registration for domain={domain}")
            return entry
        
        # Create new entry
        entry = DomainEntry(
            domain=domain,
            site_map=site_map,
            auto_load=auto_load
        )
        
        # Add aliases
        if aliases:
            for alias in aliases:
                entry.add_alias(alias)
                self.aliases[alias.replace("www.", "").lower()] = domain
        
        # Register primary domain
        self.domains[domain] = entry
        self.aliases[domain] = domain
        
        logger.info(
            f"📝 Domain registered | domain={domain} | "
            f"aliases={len(entry.aliases)} | auto_load={auto_load}"
        )
        
        return entry
    
    def get_domain_map(self, domain: str) -> Optional[DomainEntry]:
        """
        Get a domain entry from the registry.
        
        Resolves aliases to primary domains.
        
        Args:
            domain: Domain name (may be an alias)
            
        Returns:
            DomainEntry if found, None otherwise
        """
        # Normalize domain
        domain = domain.replace("www.", "").lower()
        
        # Check direct match
        if domain in self.domains:
            entry = self.domains[domain]
            entry.access_count += 1
            return entry
        
        # Check alias mapping
        if domain in self.aliases:
            primary_domain = self.aliases[domain]
            entry = self.domains.get(primary_domain)
            if entry:
                entry.access_count += 1
                return entry
        
        logger.debug(f"No domain map found for domain={domain}")
        return None
    
    def get_site_map(self, domain: str) -> Optional[Any]:
        """
        Get the SiteMap for a domain.
        
        Args:
            domain: Domain name
            
        Returns:
            SiteMap if loaded, None otherwise
        """
        entry = self.get_domain_map(domain)
        if entry:
            return entry.site_map
        return None
    
    def update_site_map(self, domain: str, site_map: Any) -> bool:
        """
        Update the SiteMap for a domain.
        
        Args:
            domain: Domain name
            site_map: New SiteMap object
            
        Returns:
            True if update successful
        """
        entry = self.get_domain_map(domain)
        if not entry:
            logger.warning(f"Cannot update site map: domain={domain} not registered")
            return False
        
        entry.site_map = site_map
        logger.debug(f"Updated site map for domain={domain}")
        return True
    
    def unregister_domain(self, domain: str) -> bool:
        """
        Remove a domain from the registry.
        
        Args:
            domain: Domain name to remove
            
        Returns:
            True if removal successful
        """
        domain = domain.replace("www.", "").lower()
        
        if domain not in self.domains:
            return False
        
        entry = self.domains[domain]
        
        # Remove aliases
        for alias in entry.aliases:
            self.aliases.pop(alias, None)
        self.aliases.pop(domain, None)
        
        # Remove domain
        del self.domains[domain]
        
        logger.info(f"🗑️ Domain unregistered | domain={domain}")
        return True
    
    def get_all_domains(self) -> List[str]:
        """Get list of all registered domains"""
        return list(self.domains.keys())
    
    def get_registered_count(self) -> int:
        """Get count of registered domains"""
        return len(self.domains)
    
    def is_registered(self, domain: str) -> bool:
        """Check if a domain is registered"""
        domain = domain.replace("www.", "").lower()
        return domain in self.domains or domain in self.aliases
    
    def extract_domain_from_url(self, url: str) -> Optional[str]:
        """
        Extract domain from a URL.
        
        Args:
            url: Full URL
            
        Returns:
            Extracted domain (normalized), or None if extraction fails
        """
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.", "").lower()
            
            # Remove port if present
            if ":" in domain:
                domain = domain.split(":")[0]
            
            return domain
        except Exception as e:
            logger.warning(f"Failed to extract domain from URL: {e}")
            return None
    
    def resolve_domain_from_url(self, url: str) -> Optional[DomainEntry]:
        """
        Resolve domain entry from a URL.
        
        Combines domain extraction and lookup.
        
        Args:
            url: Full URL
            
        Returns:
            DomainEntry if found, None otherwise
        """
        domain = self.extract_domain_from_url(url)
        if not domain:
            return None
        
        return self.get_domain_map(domain)
    
    def get_registry_summary(self) -> Dict[str, Any]:
        """
        Get summary information about the registry.
        
        Returns:
            Dictionary with registry statistics
        """
        total_access = sum(e.access_count for e in self.domains.values())
        loaded_maps = sum(1 for e in self.domains.values() if e.site_map is not None)
        
        return {
            "total_domains": len(self.domains),
            "total_aliases": len(self.aliases),
            "loaded_maps": loaded_maps,
            "total_access_count": total_access,
            "domains": [
                {
                    "domain": e.domain,
                    "has_map": e.site_map is not None,
                    "access_count": e.access_count,
                    "aliases": len(e.aliases)
                }
                for e in self.domains.values()
            ]
        }
    
    def clear_registry(self) -> None:
        """Clear all registered domains"""
        self.domains.clear()
        self.aliases.clear()
        logger.info("PageRegistry cleared")


# Singleton instance
_page_registry_instance: Optional[PageRegistry] = None


def get_page_registry() -> PageRegistry:
    """Get or create the singleton PageRegistry instance"""
    global _page_registry_instance
    if _page_registry_instance is None:
        _page_registry_instance = PageRegistry()
    return _page_registry_instance


# Convenience functions
def register_domain(
    domain: str,
    site_map: Optional[Any] = None,
    aliases: Optional[List[str]] = None
) -> DomainEntry:
    """Register a domain"""
    registry = get_page_registry()
    return registry.register_domain(domain, site_map, aliases)


def get_domain_map(domain: str) -> Optional[DomainEntry]:
    """Get domain map"""
    registry = get_page_registry()
    return registry.get_domain_map(domain)


def resolve_domain_from_url(url: str) -> Optional[DomainEntry]:
    """Resolve domain from URL"""
    registry = get_page_registry()
    return registry.resolve_domain_from_url(url)

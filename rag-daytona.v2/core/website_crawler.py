"""
Website Crawler Module for Deep URL Extraction

Provides functionality to:
1. Crawl websites to discover all pages (sitemap-style)
2. Extract structured content from multiple pages
3. Generate comprehensive multi-page READMEs
"""

import asyncio
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, urldefrag
from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass, field
import logging
from datetime import datetime
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


def _safe_page_title(soup: BeautifulSoup) -> str:
    """
    Safely extract page title for malformed/dynamic HTML where soup.title.string may be None.
    """
    try:
        if soup.title:
            text = (soup.title.get_text(" ", strip=True) or "").strip()
            if text:
                return text
    except Exception:
        pass
    return "No title"


@dataclass
class DiscoveredPage:
    """Represents a discovered page during crawling."""
    url: str
    title: str = ""
    depth: int = 0
    status_code: int = 200
    content_type: str = "text/html"
    word_count: int = 0
    has_forms: bool = False
    has_images: bool = False
    parent_url: Optional[str] = None
    discovered_at: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "depth": self.depth,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "word_count": self.word_count,
            "has_forms": self.has_forms,
            "has_images": self.has_images,
            "parent_url": self.parent_url,
            "discovered_at": self.discovered_at.isoformat()
        }


@dataclass
class PageContent:
    """Full content extracted from a page."""
    url: str
    title: str
    meta_description: str
    headings: Dict[str, List[str]]
    content_text: str
    links: List[Dict[str, str]]
    images: List[Dict[str, str]]
    forms: List[Dict[str, Any]]
    tables: List[Dict[str, Any]]
    markdown_content: str = ""
    extracted_at: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "meta_description": self.meta_description,
            "headings": self.headings,
            "content_text": self.content_text,
            "markdown_content": self.markdown_content,
            "links": self.links,
            "images": self.images,
            "forms": self.forms,
            "tables": self.tables,
            "links_count": len(self.links),
            "images_count": len(self.images),
            "forms_count": len(self.forms),
            "tables_count": len(self.tables),
            "extracted_at": self.extracted_at.isoformat()
        }


class WebsiteCrawler:
    """
    Crawls websites to discover pages and extract content.
    
    Features:
    - Configurable crawl depth
    - Domain-restricted crawling
    - Rate limiting
    - Duplicate detection
    - Content filtering
    """
    
    def __init__(
        self,
        max_depth: int = 2,
        max_pages: int = 50,
        same_domain_only: bool = True,
        use_sitemap: bool = True,
        rate_limit_delay: float = 0.5,
        timeout: float = 30.0,
        respect_robots: bool = False  # Future: implement robots.txt support
    ):
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.same_domain_only = same_domain_only
        self.use_sitemap = use_sitemap
        self.rate_limit_delay = rate_limit_delay
        self.timeout = timeout
        self.respect_robots = respect_robots
        
        # Tracking
        self.visited_urls: Set[str] = set()
        self.discovered_pages: List[DiscoveredPage] = []
        self.page_contents: Dict[str, PageContent] = {}
        
        # HTTP client
        self.client: Optional[httpx.AsyncClient] = None
        
        # User agent
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
    
    def _normalize_url(self, url: str) -> str:
        """Normalize URL by removing fragments and trailing slashes."""
        url = urldefrag(url)[0]  # Remove fragment
        url = url.rstrip('/')  # Remove trailing slash for consistency
        return url
    
    def _is_same_domain(self, url: str, base_domain: str) -> bool:
        """Check if URL belongs to the same domain."""
        try:
            parsed = urlparse(url)
            a = parsed.netloc.lower().replace("www.", "")
            b = base_domain.lower().replace("www.", "")
            return a == b
        except:
            return False

    async def _discover_sitemap_urls(self, start_url: str, base_domain: str) -> List[str]:
        """Discover candidate URLs from robots.txt and sitemap XML files."""
        candidates: Set[str] = set()
        parsed = urlparse(start_url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        # Known sitemap locations
        for path in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/sitemap/sitemap.xml"):
            candidates.add(urljoin(base, path))

        # Parse robots.txt for Sitemap directives
        try:
            robots_url = urljoin(base, "/robots.txt")
            robots_result = await self._fetch_page(robots_url, allow_non_html=True)
            if robots_result and robots_result[1]:
                for line in robots_result[1].splitlines():
                    if line.lower().startswith("sitemap:"):
                        maybe = line.split(":", 1)[1].strip()
                        if maybe:
                            candidates.add(maybe)
        except Exception:
            pass

        discovered: Set[str] = set()
        queue = list(candidates)
        seen_maps: Set[str] = set()

        while queue and len(discovered) < self.max_pages * 5:
            sitemap_url = queue.pop(0)
            if sitemap_url in seen_maps:
                continue
            seen_maps.add(sitemap_url)
            try:
                result = await self._fetch_page(sitemap_url, allow_non_html=True)
                if not result or not result[1]:
                    continue
                status_code, xml_text, _ = result
                if status_code >= 400:
                    continue

                root = ET.fromstring(xml_text)
                ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

                sitemap_locs = root.findall(".//sm:sitemap/sm:loc", ns) or root.findall(".//sitemap/loc")
                if sitemap_locs:
                    for loc in sitemap_locs:
                        loc_text = (loc.text or "").strip()
                        if loc_text and loc_text not in seen_maps:
                            queue.append(loc_text)

                url_locs = root.findall(".//sm:url/sm:loc", ns) or root.findall(".//url/loc")
                for loc in url_locs:
                    url_text = (loc.text or "").strip()
                    if url_text and self._should_crawl(url_text, base_domain):
                        discovered.add(self._normalize_url(url_text))
                        if len(discovered) >= self.max_pages * 5:
                            break
            except Exception:
                continue

        return list(discovered)
    
    def _should_crawl(self, url: str, base_domain: str) -> bool:
        """Determine if a URL should be crawled."""
        # Normalize
        url = self._normalize_url(url)
        
        # Already visited
        if url in self.visited_urls:
            return False
        
        # Parse URL
        try:
            parsed = urlparse(url)
        except:
            return False
        
        # Must be HTTP/HTTPS
        if parsed.scheme not in ('http', 'https'):
            return False
        
        # Skip file extensions that aren't HTML
        skip_extensions = {
            '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp',
            '.css', '.js', '.xml', '.json', '.zip', '.tar', '.gz',
            '.mp3', '.mp4', '.avi', '.mov', '.doc', '.docx', '.xls', '.xlsx',
            '.ppt', '.pptx', '.zip', '.rar', '.exe', '.dmg', '.pkg'
        }
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in skip_extensions):
            return False
        
        # Skip common non-content URLs
        skip_patterns = [
            'mailto:', 'tel:', 'javascript:', 'data:',
            '/wp-json/', '/wp-content/uploads/',
            '/assets/', '/static/', '/images/', '/img/',
            '/download/', '/uploads/', '/media/',
            '?share=', 'print=', 'feed=', 'rss', 'atom',
            '/api/', '/graphql', '/ws/', '/wss/',
            'facebook.com', 'twitter.com', 'linkedin.com', 
            'youtube.com', 'instagram.com', 'tiktok.com',
            'share?', 'logout', 'login?', 'cart?', 'checkout?'
        ]
        url_lower = url.lower()
        if any(pattern in url_lower for pattern in skip_patterns):
            return False
        
        # Domain check
        if self.same_domain_only and not self._is_same_domain(url, base_domain):
            return False
        
        return True
    
    def _extract_links(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        """Extract all valid links from a page."""
        links = []
        base_domain = urlparse(base_url).netloc
        
        for anchor in soup.find_all('a', href=True):
            href = anchor['href'].strip()
            
            # Skip empty or javascript links
            if not href or href.startswith(('javascript:', '#', 'data:')):
                continue
            
            # Resolve relative URLs
            absolute_url = urljoin(base_url, href)
            
            # Check if should crawl
            if self._should_crawl(absolute_url, base_domain):
                normalized = self._normalize_url(absolute_url)
                if normalized not in self.visited_urls:
                    links.append(normalized)
        
        return links

    def _classify_anchor_zone(self, anchor) -> str:
        """Infer where an anchor lives in the page layout."""
        for parent in anchor.parents:
            name = (getattr(parent, "name", "") or "").lower()
            if name in ("nav",):
                return "nav"
            if name in ("aside",):
                return "sidebar"
            if name in ("footer",):
                return "footer"
            if name in ("header",):
                return "header"
        return "main"

    def _interactable_priority(self, href: str, text: str, zone: str) -> Tuple[int, str]:
        """Score interactables so navbar/sidebar/footer core routes rank first."""
        reasons: List[str] = []
        score = {
            "nav": 95,
            "sidebar": 88,
            "header": 82,
            "footer": 68,
            "main": 60
        }.get(zone, 55)
        reasons.append(f"zone={zone}")

        low = f"{text} {href}".lower()
        core_patterns = [
            "about", "contact", "pricing", "plan", "product", "products",
            "category", "categories", "collection", "shop", "services",
            "solutions", "features", "docs", "documentation", "support",
            "help", "faq", "blog", "careers", "company", "enterprise",
            "men", "women", "kids", "brands", "new-arrivals", "best-seller"
        ]
        if any(p in low for p in core_patterns):
            score += 26
            reasons.append("core-route")

        transactional_noise = [
            "login", "sign in", "sign-in", "signup", "register", "account", "profile",
            "cart", "checkout", "wishlist", "orders", "privacy", "terms", "cookie",
            "policy", "logout"
        ]
        if any(p in low for p in transactional_noise):
            score -= 42
            reasons.append("utility/noise")

        if "?" in href:
            score -= 8
            reasons.append("query-url")

        if len(text.strip()) < 2:
            score -= 10
            reasons.append("weak-label")

        return score, ", ".join(reasons)

    def _path_depth(self, url: str) -> int:
        """Return URL path depth: / -> 0, /a -> 1, /a/b -> 2."""
        try:
            p = urlparse(url).path or "/"
            segs = [s for s in p.split("/") if s]
            return len(segs)
        except Exception:
            return 99

    def _is_primary_main_link(self, start_url: str, href: str, text: str) -> bool:
        """
        Keep only high-signal main-content links from homepage (hero/category CTAs),
        and skip deep/article/detail URLs that create noisy crawl expansions.
        """
        parsed = urlparse(href)
        path = (parsed.path or "/").lower()
        label = (text or "").strip().lower()
        if not label:
            return False

        # Exclude obvious detail/article/deep URLs in main area.
        segments = [s for s in path.split("/") if s]
        if len(segments) >= 3:
            return False
        if any(tok in path for tok in ["/blog/", "/newsroom/", "/case-studies/", "/press/", "/stories/"]):
            return False
        if any(tok in label for tok in ["read more", "learn more", "view details", "see more"]):
            return False

        core_tokens = [
            "pricing", "product", "products", "platform", "solutions", "features",
            "docs", "documentation", "support", "help", "contact", "about",
            "enterprise", "shop", "category", "categories", "collection", "collections",
            "men", "women", "kids", "brands", "new arrivals", "best seller"
        ]
        if any(tok in (path + " " + label) for tok in core_tokens):
            return True

        # If it is a root/one-segment URL with a concise CTA-like label, allow it.
        if len(segments) <= 1 and len(label) <= 40:
            return True

        return False
    
    async def _fetch_page(self, url: str, allow_non_html: bool = False) -> Optional[Tuple[int, str, Dict[str, str]]]:
        """Fetch a single page and return status code, content, and headers."""
        try:
            response = await self.client.get(url, headers=self.headers, follow_redirects=True)
            content_type = response.headers.get('content-type', '').lower()
            
            # Only process HTML content unless explicitly allowed
            if not allow_non_html and 'text/html' not in content_type and 'application/xhtml' not in content_type:
                return response.status_code, "", dict(response.headers)
            
            return response.status_code, response.text, dict(response.headers)
        except httpx.TimeoutException:
            logger.warning(f"Timeout fetching {url}")
            return None
        except Exception as e:
            logger.warning(f"Error fetching {url}: {e}")
            return None
    
    async def _process_page(self, url: str, depth: int, parent_url: Optional[str] = None) -> Optional[DiscoveredPage]:
        """Process a single page and extract basic information."""
        url = self._normalize_url(url)
        
        if url in self.visited_urls:
            return None
        
        self.visited_urls.add(url)
        
        # Fetch page
        result = await self._fetch_page(url)
        if not result:
            return None
        
        status_code, html, headers = result
        
        # Skip non-HTML content
        if not html:
            return DiscoveredPage(
                url=url,
                depth=depth,
                status_code=status_code,
                content_type=headers.get('content-type', 'unknown'),
                parent_url=parent_url
            )
        
        # Parse HTML
        soup = BeautifulSoup(html, 'html.parser')
        
        # Extract basic info
        title = _safe_page_title(soup)
        
        # Count words
        text_content = soup.get_text(separator=' ', strip=True)
        word_count = len(text_content.split())
        
        # Check for forms
        has_forms = bool(soup.find_all('form'))
        
        # Check for images
        has_images = bool(soup.find_all('img'))
        
        page = DiscoveredPage(
            url=url,
            title=title,
            depth=depth,
            status_code=status_code,
            content_type=headers.get('content-type', 'text/html'),
            word_count=word_count,
            has_forms=has_forms,
            has_images=has_images,
            parent_url=parent_url
        )
        
        self.discovered_pages.append(page)
        
        # Rate limiting
        await asyncio.sleep(self.rate_limit_delay)
        
        return page
    
    async def crawl(self, start_url: str, seed_urls: Optional[List[str]] = None) -> List[DiscoveredPage]:
        """
        Crawl a website starting from the given URL.
        
        Returns list of discovered pages (limited by max_pages and max_depth).
        """
        # Reset state
        self.visited_urls = set()
        self.discovered_pages = []
        self.page_contents = {}
        
        # Normalize start URL
        start_url = self._normalize_url(start_url)
        base_domain = urlparse(start_url).netloc
        
        logger.info(f"Starting crawl from {start_url} (domain: {base_domain})")
        
        # Initialize HTTP client
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            self.client = client
            
            # Queue of (url, depth, parent_url) to process
            queue: asyncio.Queue = asyncio.Queue()
            await queue.put((start_url, 0, None))

            # Seed crawl with prioritized core URLs discovered from homepage interactables.
            for seed in (seed_urls or []):
                normalized_seed = self._normalize_url(seed)
                if normalized_seed and normalized_seed != start_url and normalized_seed not in self.visited_urls:
                    await queue.put((normalized_seed, 1, start_url))

            # Seed from sitemap when available; this helps JS-heavy sites.
            if self.use_sitemap:
                sitemap_urls = await self._discover_sitemap_urls(start_url, base_domain)
                if sitemap_urls:
                    logger.info(f"Sitemap discovery found {len(sitemap_urls)} candidate pages")
                    for url in sitemap_urls[: self.max_pages * 2]:
                        if url not in self.visited_urls:
                            await queue.put((url, 1, start_url))
            
            # Process queue
            tasks = []
            while len(self.visited_urls) < self.max_pages:
                try:
                    url, depth, parent_url = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    break
                
                if url in self.visited_urls:
                    continue
                
                # Process page
                page = await self._process_page(url, depth, parent_url)
                
                # If successful and we haven't reached max depth, extract links
                if page and depth < self.max_depth:
                    # Re-fetch to get links (we could optimize this)
                    result = await self._fetch_page(url)
                    if result and result[1]:
                        soup = BeautifulSoup(result[1], 'html.parser')
                        links = self._extract_links(soup, url)
                        
                        for link in links:
                            if len(self.visited_urls) >= self.max_pages:
                                break
                            if link not in self.visited_urls:
                                await queue.put((link, depth + 1, url))
                
                if len(self.visited_urls) >= self.max_pages:
                    break
        
        self.client = None
        logger.info(f"Crawl complete. Discovered {len(self.discovered_pages)} pages.")
        
        return self.discovered_pages
    
    async def extract_full_content(self, urls: List[str]) -> Dict[str, PageContent]:
        """
        Extract full content from a list of URLs.
        
        Returns dict of url -> PageContent.
        """
        self.page_contents = {}
        
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            self.client = client
            
            for url in urls:
                url = self._normalize_url(url)
                logger.info(f"Extracting content from {url}")
                
                result = await self._fetch_page(url)
                if not result or not result[1]:
                    continue
                
                status_code, html, headers = result
                soup = BeautifulSoup(html, 'html.parser')
                
                # Extract title
                title = _safe_page_title(soup)
                
                # Extract meta description
                meta_desc = ""
                meta_tag = soup.find('meta', attrs={'name': 'description'}) or \
                          soup.find('meta', attrs={'property': 'og:description'})
                if meta_tag:
                    meta_desc = meta_tag.get('content', '')
                
                # Extract headings
                headings = {
                    'h1': [h.get_text(strip=True) for h in soup.find_all('h1')],
                    'h2': [h.get_text(strip=True) for h in soup.find_all('h2')],
                    'h3': [h.get_text(strip=True) for h in soup.find_all('h3')],
                    'h4': [h.get_text(strip=True) for h in soup.find_all('h4')]
                }
                
                # Extract main content text
                # Remove script and style elements
                for script in soup(['script', 'style', 'noscript']):
                    script.decompose()
                
                # Get text from main content areas
                content_text = ""
                for selector in ['main', 'article', '[role="main"]', '.content', '#content', 'body']:
                    content_area = soup.select_one(selector)
                    if content_area:
                        content_text = content_area.get_text(separator='\n', strip=True)
                        break

                # Build markdown-like content (richer, similar to groq.md style)
                markdown_lines: List[str] = []
                body = soup.body or soup
                for node in body.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'a', 'pre', 'code']):
                    text = node.get_text(" ", strip=True)
                    if not text:
                        continue
                    if node.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
                        level = int(node.name[1])
                        markdown_lines.append(f"{'#' * level} {text}")
                    elif node.name == 'a':
                        href = node.get('href', '').strip()
                        if href:
                            markdown_lines.append(f"[{text}]({urljoin(url, href)})")
                        else:
                            markdown_lines.append(text)
                    elif node.name == 'li':
                        markdown_lines.append(f"* {text}")
                    elif node.name == 'code':
                        markdown_lines.append(f"`{text}`")
                    else:
                        markdown_lines.append(text)
                markdown_content = "\n".join(markdown_lines)
                
                # Extract links
                links = []
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    text = a.get_text(strip=True)
                    if text and len(text) < 200:  # Skip long garbage text
                        links.append({
                            'text': text,
                            'href': href,
                            'is_external': not self._is_same_domain(urljoin(url, href), urlparse(url).netloc)
                        })
                
                # Extract images
                images = []
                for img in soup.find_all('img'):
                    src = img.get('src', img.get('data-src', ''))
                    alt = img.get('alt', '')
                    if src:
                        images.append({
                            'src': urljoin(url, src),
                            'alt': alt
                        })
                
                # Extract forms
                forms = []
                for form in soup.find_all('form'):
                    form_data = {
                        'action': form.get('action', ''),
                        'method': form.get('method', 'get'),
                        'inputs': []
                    }
                    for inp in form.find_all(['input', 'textarea', 'select']):
                        form_data['inputs'].append({
                            'type': inp.get('type', inp.name),
                            'name': inp.get('name', ''),
                            'placeholder': inp.get('placeholder', '')
                        })
                    forms.append(form_data)
                
                # Extract tables
                tables = []
                for table in soup.find_all('table'):
                    table_data = {
                        'headers': [],
                        'rows': []
                    }
                    headers = table.find_all('th')
                    if headers:
                        table_data['headers'] = [h.get_text(strip=True) for h in headers]
                    for row in table.find_all('tr'):
                        cells = row.find_all(['td', 'th'])
                        if cells:
                            table_data['rows'].append([c.get_text(strip=True) for c in cells])
                    if table_data['rows']:
                        tables.append(table_data)
                
                self.page_contents[url] = PageContent(
                    url=url,
                    title=title,
                    meta_description=meta_desc,
                    headings=headings,
                    content_text=content_text,
                    links=links,
                    images=images,
                    forms=forms,
                    tables=tables,
                    markdown_content=markdown_content
                )
                
                # Rate limiting
                await asyncio.sleep(self.rate_limit_delay)
        
        self.client = None
        return self.page_contents


def generate_readme(
    domain: str,
    pages: Dict[str, PageContent],
    include_structural_guide: bool = True
) -> str:
    """
    Generate a comprehensive README from extracted page content.
    
    This creates a multi-pillar structure similar to groq.md.
    """
    lines = []
    
    # Header
    lines.append(f"# {domain}: Master Structural & Technical Guide")
    lines.append("")
    lines.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"Total Pages: {len(pages)}")
    lines.append("")
    lines.append("---")
    lines.append("")
    
    # Each page as a PILLAR
    for i, (url, page) in enumerate(pages.items(), 1):
        lines.append(f"## PILLAR {i}: {url}")
        lines.append("")
        lines.append(f"Title: {page.title}")
        lines.append("")
        lines.append(f"URL Source: {url}")
        lines.append("")
        
        if page.meta_description:
            lines.append(f"Meta Description: {page.meta_description}")
            lines.append("")
        
        # Headings
        if any(page.headings.values()):
            lines.append("Headings Structure:")
            lines.append("")
            for level, headings in page.headings.items():
                if headings:
                    lines.append(f"{level.upper()}:")
                    for h in headings[:10]:  # Limit
                        lines.append(f"- {h}")
                    lines.append("")
        
        # Main Content
        if page.markdown_content:
            lines.append("Markdown Content:")
            lines.append(page.markdown_content[:50000])
            lines.append("")
        elif page.content_text:
            lines.append("Content:")
            lines.append("")
            # Clean and truncate content
            content = page.content_text[:50000]
            lines.append(content)
            lines.append("")
        
        # Links
        if page.links:
            lines.append("Navigation Links:")
            lines.append("")
            internal_links = [l for l in page.links if not l.get('is_external')][:20]
            for link in internal_links:
                lines.append(f"- [{link['text']}]({link['href']})")
            lines.append("")
        
        # Forms
        if page.forms:
            lines.append("Forms:")
            lines.append("")
            for form in page.forms:
                lines.append(f"- Form (method: {form['method']})")
                for inp in form['inputs'][:5]:
                    lines.append(f"  - {inp['name']} ({inp['type']})")
            lines.append("")
        
        # Tables
        if page.tables:
            lines.append("Data Tables:")
            lines.append("")
            for table in page.tables[:2]:
                if table['headers']:
                    lines.append("| " + " | ".join(table['headers']) + " |")
                    lines.append("| " + " | ".join(["---"] * len(table['headers'])) + " |")
                for row in table['rows'][:10]:
                    lines.append("| " + " | ".join(row) + " |")
                lines.append("")
        
        lines.append("---")
        lines.append("")
    
    # Structural Guide Section (if enabled)
    if include_structural_guide:
        lines.append("## Structural Navigation Guide")
        lines.append("")
        lines.append("### Page Hierarchy")
        lines.append("")
        
        for url, page in pages.items():
            lines.append(f"#### {page.title}")
            lines.append(f"- **URL:** {url}")
            lines.append(f"- **Purpose:** {page.meta_description[:100] if page.meta_description else 'N/A'}...")
            lines.append(f"- **Key Actions:** Forms: {len(page.forms)}, Links: {len(page.links)}")
            lines.append("")
        
        lines.append("---")
        lines.append("")
    
    return "\n".join(lines)


def _infer_strategy_for_page(url: str, title: str) -> Tuple[str, str, List[str], Dict[str, List[str]]]:
    low = f"{title} {url}".lower()
    if any(k in low for k in ["pricing", "plan", "subscription"]):
        return (
            "extraction",
            "Navigate to pricing and evaluate plans",
            ["Open main navigation", "Go to pricing", "Compare plans", "Capture key pricing details"],
            {"capture_pricing": ["pricing_page_loaded"]}
        )
    if any(k in low for k in ["product", "shop", "category", "collection", "men", "women", "kids", "brands"]):
        return (
            "extraction",
            "Navigate product discovery paths and capture category structure",
            ["Open navigation menu", "Choose target category", "Open listing page", "Capture filters and product cards"],
            {"open_product_listing": ["category_selected"]}
        )
    if any(k in low for k in ["contact", "support", "help", "faq"]):
        return (
            "interaction",
            "Navigate support/contact flows",
            ["Open support or contact section", "Verify support options", "Capture contact channels"],
            {"submit_contact_form": ["contact_page_loaded"]}
        )
    return (
        "navigation",
        "Navigate core website sections",
        ["Open homepage", "Use top navigation", "Open target section", "Verify target page context"],
        {"open_target_section": ["main_nav_visible"]}
    )


def generate_hivemind_payload(
    domain: str,
    pages: Dict[str, PageContent],
    max_hints_per_page: int = 30
) -> List[Dict[str, Any]]:
    """
    Convert extracted pages into extract_payload-compatible JSON documents.
    """
    docs: List[Dict[str, Any]] = []
    seen_hints: Set[str] = set()
    seen_strategy: Set[str] = set()

    for url, page in pages.items():
        action, concept, sequence, blocking_rules = _infer_strategy_for_page(url, page.title)
        skey = f"{domain}:{action}:{url}"
        if skey not in seen_strategy:
            seen_strategy.add(skey)
            docs.append({
                "doc_type": "Strategy_Sequence",
                "domain": domain,
                "action": action,
                "sequence": sequence,
                "constraints_order": ["target_section", "page_context"],
                "blocking_rules": blocking_rules,
                "example_url": url,
                "text": f"Strategy for {action} on {domain}: {concept}. URL: {url}",
                "concept": concept
            })

        docs.append({
            "doc_type": "Website_Map",
            "domain": domain,
            "url": url,
            "concept": concept,
            "sequence": sequence,
            "blocking_rules": ["ensure_page_loaded_before_interaction", "verify_target_elements_visible"],
            "action_script": "await page.goto('<url>'); // navigate using mapped core links"
        })

        internal_links = [l for l in page.links if not l.get("is_external")]
        for link in internal_links[:max_hints_per_page]:
            text = (link.get("text") or "").strip()
            href = (link.get("href") or "").strip()
            if not text or not href:
                continue
            if len(text) > 120:
                continue
            abs_href = urljoin(url, href)
            parsed = urlparse(abs_href)
            if parsed.scheme not in ("http", "https"):
                continue

            path = parsed.path or "/"
            selector = "a[href='/']" if path == "/" else f"a[href*='{path}']"

            zone = "main"
            low = f"{text} {href}".lower()
            if any(x in low for x in ["footer", "privacy", "terms", "cookie"]):
                zone = "footer"
            elif any(x in low for x in ["menu", "category", "shop", "product", "docs", "support"]):
                zone = "nav"
            elif any(x in low for x in ["filter", "sort", "apply"]):
                zone = "sidebar"

            hkey = f"{domain}:{selector}:{text.lower()}"
            if hkey in seen_hints:
                continue
            seen_hints.add(hkey)

            docs.append({
                "doc_type": "Visual_Hint",
                "domain": domain,
                "selector": selector,
                "keyword_match": text.lower(),
                "element_type": "a",
                "text_pattern": text,
                "zone": zone,
                "description": f"Navigate to '{text}' ({abs_href})"
            })

    return docs


# Convenience functions for API usage
async def discover_website_pages(
    start_url: str,
    max_depth: int = 2,
    max_pages: int = 50,
    same_domain_only: bool = True
) -> List[Dict[str, Any]]:
    """Discover all pages on a website."""
    crawler = WebsiteCrawler(
        max_depth=max_depth,
        max_pages=max_pages,
        same_domain_only=same_domain_only
    )
    pages = await crawler.crawl(start_url)
    return [p.to_dict() for p in pages]


async def discover_homepage_interactables(
    start_url: str,
    same_domain_only: bool = True,
    max_depth: int = 2,
    max_links: int = 80
) -> Dict[str, Any]:
    """
    Extract crucial interactables from homepage DOM zones and prioritize URLs
    for focused crawling.
    """
    crawler = WebsiteCrawler(
        max_depth=1,
        max_pages=max_links,
        same_domain_only=same_domain_only,
        use_sitemap=False
    )

    start_url = crawler._normalize_url(start_url)
    base_domain = urlparse(start_url).netloc
    interactables: List[Dict[str, Any]] = []
    seen_url: Set[str] = set()

    async with httpx.AsyncClient(timeout=crawler.timeout, follow_redirects=True) as client:
        crawler.client = client
        result = await crawler._fetch_page(start_url)
        if not result or not result[1]:
            return {"start_url": start_url, "total_interactables": 0, "interactables": [], "recommended_urls": [start_url]}

        _, html, _ = result
        soup = BeautifulSoup(html, "html.parser")

        zones = [
            ("nav", "nav a[href], header nav a[href], [role='navigation'] a[href]"),
            ("sidebar", "aside a[href], [role='complementary'] a[href]"),
            ("footer", "footer a[href]"),
            ("header", "header a[href]"),
            ("main", "main a[href], article a[href], body a[href]")
        ]

        for zone_name, selector in zones:
            for anchor in soup.select(selector):
                href = (anchor.get("href") or "").strip()
                text = anchor.get_text(" ", strip=True)
                if not href:
                    continue
                abs_url = urljoin(start_url, href)
                if not crawler._should_crawl(abs_url, base_domain):
                    continue
                normalized = crawler._normalize_url(abs_url)
                if normalized in seen_url:
                    continue
                path_depth = crawler._path_depth(normalized)
                if path_depth > max_depth:
                    continue

                detected_zone = crawler._classify_anchor_zone(anchor) or zone_name
                if detected_zone == "main" and not crawler._is_primary_main_link(start_url, normalized, text):
                    continue
                score, reason = crawler._interactable_priority(normalized, text, detected_zone)
                interactables.append({
                    "url": normalized,
                    "text": text or normalized,
                    "zone": detected_zone,
                    "path_depth": path_depth,
                    "priority": score,
                    "reason": reason
                })
                seen_url.add(normalized)

                if len(interactables) >= max_links:
                    break
            if len(interactables) >= max_links:
                break

        # Button-driven destinations on homepage (onclick/formaction/data-href)
        for btn in soup.select("button, input[type='button'], input[type='submit']"):
            text = btn.get_text(" ", strip=True) if hasattr(btn, "get_text") else ""
            candidate_urls: List[str] = []
            for attr in ("formaction", "data-href", "data-url"):
                v = (btn.get(attr) or "").strip()
                if v:
                    candidate_urls.append(v)
            onclick = (btn.get("onclick") or "").strip()
            if onclick:
                m = re.search(r"""(?:location(?:\.href)?\s*=\s*|window\.open\()\s*['"]([^'"]+)['"]""", onclick)
                if m:
                    candidate_urls.append(m.group(1))

            for href in candidate_urls:
                abs_url = urljoin(start_url, href)
                if not crawler._should_crawl(abs_url, base_domain):
                    continue
                normalized = crawler._normalize_url(abs_url)
                if normalized in seen_url:
                    continue
                path_depth = crawler._path_depth(normalized)
                if path_depth > max_depth:
                    continue
                score, reason = crawler._interactable_priority(normalized, text or "button", "main")
                interactables.append({
                    "url": normalized,
                    "text": text or "button",
                    "zone": "main",
                    "path_depth": path_depth,
                    "priority": score,
                    "reason": f"button,{reason}"
                })
                seen_url.add(normalized)
                if len(interactables) >= max_links:
                    break
            if len(interactables) >= max_links:
                break

    crawler.client = None
    interactables.sort(key=lambda x: x["priority"], reverse=True)
    recommended_urls = [start_url] + [
        i["url"] for i in interactables
        if i["priority"] >= 50 and i.get("path_depth", crawler._path_depth(i["url"])) <= max_depth
    ]
    recommended_urls = list(dict.fromkeys(recommended_urls))[: min(max_links, 40)]

    # Fetch basic metadata for destination pages so UI can show "where it leads"
    destination_pages: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=crawler.timeout, follow_redirects=True) as client:
        crawler.client = client
        depth_by_url = {start_url: 0}
        for idx, it in enumerate(interactables):
            depth_by_url[it["url"]] = int(it.get("path_depth", crawler._path_depth(it["url"])))

        seen_dest: Set[str] = set()
        for target_url in recommended_urls:
            if target_url in seen_dest:
                continue
            seen_dest.add(target_url)

            if target_url == start_url:
                result = await crawler._fetch_page(target_url)
                if result and result[1]:
                    soup = BeautifulSoup(result[1], "html.parser")
                    title = _safe_page_title(soup)
                    text_content = soup.get_text(separator=" ", strip=True)
                    destination_pages.append({
                        "url": target_url,
                        "title": title,
                        "depth": 0,
                        "status_code": result[0],
                        "content_type": (result[2] or {}).get("content-type", "text/html"),
                        "word_count": len(text_content.split()),
                        "has_forms": bool(soup.find_all("form")),
                        "has_images": bool(soup.find_all("img")),
                        "parent_url": None
                    })
                continue

            if depth_by_url.get(target_url, crawler._path_depth(target_url)) > max_depth:
                continue

            result = await crawler._fetch_page(target_url)
            if not result or not result[1]:
                continue
            status_code, html, headers = result
            soup = BeautifulSoup(html, "html.parser")
            title = _safe_page_title(soup)
            text_content = soup.get_text(separator=" ", strip=True)
            destination_pages.append({
                "url": target_url,
                "title": title,
                "depth": depth_by_url.get(target_url, 1),
                "status_code": status_code,
                "content_type": (headers or {}).get("content-type", "text/html"),
                "word_count": len(text_content.split()),
                "has_forms": bool(soup.find_all("form")),
                "has_images": bool(soup.find_all("img")),
                "parent_url": start_url
            })

    crawler.client = None

    return {
        "start_url": start_url,
        "total_interactables": len(interactables),
        "interactables": interactables,
        "recommended_urls": recommended_urls,
        "destination_pages": destination_pages
    }


async def discover_core_website_pages(
    start_url: str,
    max_depth: int = 2,
    max_pages: int = 50,
    same_domain_only: bool = True,
    max_core_links: int = 40
) -> Dict[str, Any]:
    """
    Homepage-first crawl:
    1) Extract crucial interactables from first DOM sections
    2) Prioritize core URLs
    3) Crawl starting with those prioritized URLs
    """
    interactable_result = await discover_homepage_interactables(
        start_url=start_url,
        same_domain_only=same_domain_only,
        max_depth=max_depth,
        max_links=max_core_links
    )
    seed_urls = interactable_result.get("recommended_urls", [])

    crawler = WebsiteCrawler(
        max_depth=max_depth,
        max_pages=max_pages,
        same_domain_only=same_domain_only,
        use_sitemap=False
    )
    pages = await crawler.crawl(start_url, seed_urls=seed_urls)

    return {
        "homepage_interactables": interactable_result,
        "pages": [p.to_dict() for p in pages]
    }


async def extract_selected_pages(
    urls: List[str]
) -> Dict[str, Dict[str, Any]]:
    """Extract full content from selected URLs."""
    crawler = WebsiteCrawler()
    contents = await crawler.extract_full_content(urls)
    return {url: content.to_dict() for url, content in contents.items()}

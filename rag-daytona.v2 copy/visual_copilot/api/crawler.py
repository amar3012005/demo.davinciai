import asyncio
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import logging
from typing import List, Dict, Set
from pydantic import BaseModel

logger = logging.getLogger(__name__)

class CrawlRequest(BaseModel):
    url: str
    max_depth: int = 2
    max_pages: int = 50
    same_domain_only: bool = True

class ExtractRequest(BaseModel):
    urls: List[str]
    generate_readme: bool = True

class SaveReadmeRequest(BaseModel):
    readme_content: str
    domain: str
    tenant_id: str

async def _fetch_url(client: httpx.AsyncClient, url: str) -> tuple[str, str, BeautifulSoup]:
    try:
        response = await client.get(url, timeout=10.0, follow_redirects=True)
        response.raise_for_status()
        
        # Ensure it's HTML
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return url, "", None
            
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        return str(response.url), html, soup
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return url, "", None

async def crawl_website(req: CrawlRequest) -> Dict:
    start_url = req.url
    parsed_start = urlparse(start_url)
    base_domain = parsed_start.netloc

    visited_urls: Set[str] = set()
    queue = [(start_url, 0)]
    results = []

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    
    async with httpx.AsyncClient(verify=False, headers=headers, follow_redirects=True) as client:
        while queue and len(visited_urls) < req.max_pages:
            # Batch process current depth
            current_batch = queue[:10]  # Process 10 concurrent requests max
            queue = queue[10:]
            
            tasks = []
            for url, depth in current_batch:
                if url in visited_urls:
                    continue
                visited_urls.add(url)
                tasks.append((url, depth, _fetch_url(client, url)))
                
            fetched_results = await asyncio.gather(*(t for _, _, t in tasks), return_exceptions=True)
            
            for (req_url, depth, _), fetch_result in zip(tasks, fetched_results):
                if isinstance(fetch_result, Exception):
                    continue
                    
                final_url, html, soup = fetch_result
                if not soup:
                    continue
                
                # Extract simple metrics
                title = soup.title.string if soup.title else "Untitled"
                text_content = soup.get_text(separator=' ', strip=True)
                word_count = len(text_content.split())
                has_forms = len(soup.find_all('form')) > 0
                
                results.append({
                    "url": final_url,
                    "title": title.strip() if title else "",
                    "depth": depth,
                    "word_count": word_count,
                    "has_forms": has_forms,
                    "text": text_content[:5000] # store partial text for generating README later
                })

                if depth < req.max_depth and len(visited_urls) < req.max_pages:
                    for a_tag in soup.find_all('a', href=True):
                        href = a_tag['href']
                        next_url = urljoin(final_url, href).split('#')[0] # remove fragments
                        
                        parsed_next = urlparse(next_url)
                        # skip if mailto, tel, ftp, etc
                        if parsed_next.scheme not in ('http', 'https'):
                            continue
                            
                        # domain restriction
                        if req.same_domain_only and parsed_next.netloc != base_domain:
                            continue
                            
                        if next_url not in visited_urls:
                            queue.append((next_url, depth + 1))

    # sort by depth for neatness
    results.sort(key=lambda x: x["depth"])
    
    return {
        "domain": base_domain,
        "pages": results,
        "status": "success"
    }

async def extract_pages(req: ExtractRequest, crawled_cache: Dict[str, str], llm_provider) -> Dict:
    # Use the cached text from the previous crawl step
    # Make a synthetic README
    
    combined_content = ""
    for idx, url in enumerate(req.urls):
        text = crawled_cache.get(url, "")
        if text:
            combined_content += f"\n\n--- PAGE {idx+1}: {url} ---\n{text[:1000]}"
            
    # generate readme using Groq
    prompt = f"""
    You are an expert technical writer. Here are snippets of content extracted from pages belonging to a website.
    Write a comprehensive, deep README.md guide outlining the structure and purpose of this website based on the discovered pages.
    
    Content:
    {combined_content}
    
    Format output as Markdown.
    """
    
    try:
        readme = await llm_provider.generate_completion(prompt, max_tokens=2000)
    except Exception as e:
        logger.error(f"Failed to generate README: {e}")
        readme = f"# Failed to generate readme\n\nContent merged from {len(req.urls)} pages:\n\n{combined_content[:2000]}"
        
    return {
        "readme": readme,
        "status": "success"
    }

#!/usr/bin/env python3
"""
Populate Qdrant Hive Mind with chunked website data from groq.md.

Approach: Raw semantic chunks with page-level metadata.
- Splits groq.md by PILLAR sections (each = one page)
- Chunks each page into ~400-char semantic chunks
- Stores as Website_Map with proper domain extracted from URL
- Uses RAG service /api/v1/embed for embeddings (same model as search)

Run inside Docker:
    docker exec rag-local python populate_groq_hive_mind.py
"""

import requests
import json
import re
import os
import sys
import uuid
import time
from urllib.parse import urlparse

# Ensure project root on path
sys.path.insert(0, os.path.dirname(__file__))

# Configuration
QDRANT_URL = os.environ.get('QDRANT_URL', 'https://878ba705-a90e-44d7-96fc-782399dd97ae.europe-west3-0.gcp.cloud.qdrant.io:6333')
QDRANT_API_KEY = os.environ.get('QDRANT_API_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJtIn0.ao39iukssxR-CapMCzch2RRRPrEWwotD7hnOZZ2L95Q')
COLLECTION_NAME = "tara_hive"
RAG_SERVICE_URL = os.environ.get('RAG_SERVICE_URL', 'http://localhost:8003')
SOURCE_FILE = os.environ.get('SOURCE_FILE', 'Visual-co-plan/groq.md')

qdrant_headers = {
    "api-key": QDRANT_API_KEY,
    "Content-Type": "application/json"
}

# ── Helpers ──────────────────────────────────────────────────────

def get_embedding(text):
    """Get 384-D embedding from RAG service (same ONNX model used for search)."""
    try:
        resp = requests.post(
            f"{RAG_SERVICE_URL}/api/v1/embed",
            json={"text": text},
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json().get("embedding")
        print(f"  ⚠️ Embed failed ({resp.status_code}): {resp.text[:100]}")
    except Exception as e:
        print(f"  ⚠️ Embed error: {e}")
    return None


def extract_domain(url):
    """Extract domain from URL. Returns both full domain and base domain."""
    parsed = urlparse(url)
    return parsed.netloc or "groq.com"


def clean_text(text):
    """Remove markdown artifacts, images, excessive whitespace."""
    # Remove image references
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    # Remove bare URLs in image tags
    text = re.sub(r'https?://\S+\.(png|jpg|svg|webp|gif)\S*', '', text)
    # Remove copy/code button artifacts
    text = re.sub(r'\bCopy\b(?:\s+to\s+clipboard)?', '', text)
    # Collapse whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


def is_noise(text):
    """Filter out chunks that are pure noise (images, empty, legal boilerplate)."""
    if len(text) < 30:
        return True
    # Pure image references
    if text.count('Image') > 3 and len(text) < 200:
        return True
    # Cookie/privacy noise
    noise_patterns = ['cookie', 'privacy policy', 'terms of service', 'ccpa', 'gdpr']
    lower = text.lower()
    if any(p in lower for p in noise_patterns) and 'api' not in lower:
        return True
    return False


def chunk_text(text, max_chars=400, overlap=50):
    """Split text into semantic chunks at paragraph/sentence boundaries."""
    # Split by double newlines first (paragraphs)
    paragraphs = re.split(r'\n\n+', text)
    chunks = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # If paragraph itself is too long, split by sentences
        if len(para) > max_chars:
            sentences = re.split(r'(?<=[.!?])\s+', para)
            for sent in sentences:
                if len(current) + len(sent) < max_chars:
                    current += " " + sent if current else sent
                else:
                    if current:
                        chunks.append(current.strip())
                    current = sent
        elif len(current) + len(para) < max_chars:
            current += "\n" + para if current else para
        else:
            if current:
                chunks.append(current.strip())
            current = para

    if current:
        chunks.append(current.strip())

    return chunks


# ── Parser ───────────────────────────────────────────────────────

def parse_pillars(file_path):
    """Split groq.md into PILLAR sections, each representing a page."""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Split by PILLAR headers
    pillar_pattern = re.compile(r'^## PILLAR \d+:\s*(https?://\S+)', re.MULTILINE)
    matches = list(pillar_pattern.finditer(content))

    pillars = []
    for i, match in enumerate(matches):
        url = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()

        # Extract title (first line that looks like a title)
        title_match = re.search(r'^Title:\s*(.+)', body, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else url

        pillars.append({
            "url": url,
            "title": title,
            "domain": extract_domain(url),
            "body": body
        })

    return pillars


def extract_nav_links(body):
    """Extract navigation links from page body — these become nav-focused chunks."""
    link_pattern = re.compile(r'\[([^\]]+)\]\((https?://[^)]+)\)')
    links = []
    for text, url in link_pattern.findall(body):
        text = text.strip()
        if len(text) < 2 or text.startswith('Image'):
            continue
        links.append({"text": text, "url": url})
    return links


# ── Upsert ───────────────────────────────────────────────────────

def upsert_batch(points):
    """Upsert a batch of points to Qdrant."""
    try:
        resp = requests.put(
            f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points",
            headers=qdrant_headers,
            json={"points": points},
            timeout=30
        )
        if resp.status_code == 200:
            return True
        print(f"  ❌ Qdrant batch error ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        print(f"  ❌ Qdrant request error: {e}")
    return False


# ── Main ─────────────────────────────────────────────────────────

def main():
    print("🚀 Groq HiveMind Population — Raw Semantic Chunks")
    print(f"   Qdrant: {QDRANT_URL}")
    print(f"   RAG:    {RAG_SERVICE_URL}")
    print(f"   Source: {SOURCE_FILE}")
    print()

    # Health check
    try:
        r = requests.get(f"{RAG_SERVICE_URL}/health", timeout=5)
        print(f"   RAG health: {r.json().get('status', 'unknown')}")
    except Exception as e:
        print(f"   ⚠️ RAG health check failed: {e}")
        print("   Continuing anyway...")

    # Parse pillars
    pillars = parse_pillars(SOURCE_FILE)
    print(f"\n📖 Found {len(pillars)} pages (PILLARs)")

    # Skip noise pages
    skip_urls = {'groq.com/privacy-policy', 'groq.com/lpu'}
    total_points = 0
    batch = []
    batch_size = 20

    for pillar in pillars:
        url = pillar["url"]
        domain = pillar["domain"]
        title = pillar["title"]

        # Skip noise pages
        if any(skip in url for skip in skip_urls):
            print(f"  ⏩ Skipping noise page: {url}")
            continue

        print(f"\n📄 {title}")
        print(f"   URL: {url} | Domain: {domain}")

        body = clean_text(pillar["body"])

        # 1. Page summary chunk (always include)
        nav_links = extract_nav_links(pillar["body"])
        nav_text = ", ".join([l["text"] for l in nav_links[:20]])
        summary = f"{title}. Navigation: {nav_text}" if nav_text else title
        summary = summary[:500]

        vec = get_embedding(summary)
        if vec:
            batch.append({
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, url + ":summary")),
                "vector": vec,
                "payload": {
                    "doc_type": "Website_Map",
                    "domain": domain,
                    "url": url,
                    "page_type": "summary",
                    "text": summary,
                    "tenant_id": "tara",
                    "schema_version": 1,
                }
            })
            total_points += 1

        # 2. Content chunks
        chunks = chunk_text(body)
        chunk_count = 0
        for i, chunk in enumerate(chunks):
            if is_noise(chunk):
                continue

            # Prefix chunk with page context for better retrieval
            chunk_with_context = f"[{title} - {domain}] {chunk}"
            vec = get_embedding(chunk_with_context[:512])
            if not vec:
                continue

            batch.append({
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{url}:chunk:{i}")),
                "vector": vec,
                "payload": {
                    "doc_type": "Website_Map",
                    "domain": domain,
                    "url": url,
                    "page_type": "content",
                    "text": chunk,
                    "chunk_index": i,
                    "tenant_id": "tara",
                    "schema_version": 1,
                }
            })
            chunk_count += 1
            total_points += 1

            # Flush batch
            if len(batch) >= batch_size:
                print(f"   💾 Upserting batch ({len(batch)} points)...")
                upsert_batch(batch)
                batch = []

        print(f"   ✅ {chunk_count} chunks from {len(chunks)} paragraphs")

    # Final flush
    if batch:
        print(f"\n💾 Upserting final batch ({len(batch)} points)...")
        upsert_batch(batch)

    # Verify
    try:
        resp = requests.get(
            f"{QDRANT_URL}/collections/{COLLECTION_NAME}",
            headers=qdrant_headers,
            timeout=10
        )
        count = resp.json().get("result", {}).get("points_count", "?")
        print(f"\n🎉 Done! tara_hive now has {count} points (added {total_points})")
    except:
        print(f"\n🎉 Done! Added {total_points} points")


if __name__ == "__main__":
    main()

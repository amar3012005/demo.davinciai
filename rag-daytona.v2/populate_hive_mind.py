#!/usr/bin/env python3
"""
Populate Qdrant Hive Mind with 'Website Map' hints for Engel & Völkers.
Parses the 'Mega MD' file and ingests Navigation Hints into Qdrant.
Uses Universal Payload Schema v1.
"""

import requests
import json
import re
import os
import sys
import uuid
import time
from datetime import datetime

# Ensure project root on path
sys.path.insert(0, os.path.dirname(__file__))
from .models.hivemind_schema import website_map_payload

# Configuration
QDRANT_URL = "http://qdrant-n80wo80os08gswko4040wo8g.116.202.24.69.sslip.io:6333"
QDRANT_API_KEY = "WAkhOeXiD3DShev81qxn5PYKpQ9t6ufb"
COLLECTION_NAME = "tara_case_memory"
# RAG Service for Embeddings (Local Access)
RAG_SERVICE_URL = "https://localhost:8003" 


SOURCE_FILE = "/app/daytona_agent/services/rag/Engel & Völkers.md"


headers = {
    "api-key": QDRANT_API_KEY,
    "Content-Type": "application/json"
}

def get_embedding(text):
    """Get embedding from RAG service."""
    try:
        response = requests.post(
            f"{RAG_SERVICE_URL}/api/v1/embed",
            json={"text": text},
            timeout=30,
            verify=False
        )
        if response.status_code == 200:
            return response.json().get("embedding")
        else:
            print(f"⚠️ Embedding failed: {response.text}")
    except Exception as e:
        print(f"⚠️ Embedding error: {e}")
    return None

def upsert_point(tenant_id, url, concept, domain, key_selectors=[]):
    """Upsert a 'Website_Map' point to Qdrant using Universal Schema."""
    vector = get_embedding(concept)
    if not vector:
        print(f"⏩ Skipping '{concept[:30]}...' (No Vector)")
        return False
    
    # Deterministic ID based on URL
    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
    
    payload = website_map_payload(
        url=url,
        concept=concept,
        domain=domain,
        tenant_id=tenant_id,
        key_selectors=key_selectors,
    )
    payload.pop("uuid", None)  # Use deterministic ID instead
    
    point = {
        "id": point_id,
        "vector": vector,
        "payload": payload
    }


    
    try:
        response = requests.put(
            f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points",
            headers=headers,
            json={"points": [point]},
            timeout=10
        )
        if response.status_code == 200:
            print(f"✅ Indexed: {url} | '{concept[:40]}...'")
            return True
        else:
            print(f"❌ Qdrant Error {response.status_code}: {response.text}")
    except Exception as e:
        print(f"❌ Request Error: {e}")
    return False

def parse_and_ingest(file_path):
    print(f"📖 Reading {file_path}...")
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    lines = content.split('\n')
    current_pillar_url = ""
    current_context = ""
    count = 0
    
    # Regex for Markdown Link: [Result Text](URL)
    link_pattern = re.compile(r'\[([^\]]+)\]\((https?://[^)]+)\)')
    
    for line in lines:
        line = line.strip()
        
        # 1. Detect Pillar Headers (Main Landing Pages)
        if line.startswith("## PILLAR"):
            parts = line.split(":", 1)
            if len(parts) > 1:
                url = parts[1].strip()
                current_pillar_url = url
                current_context = "Main Section"
                upsert_point("tara", url, "Engel & Völkers - Main Section", "engelvoelkers.com")
                count += 1
                continue
        
        # 2. Detect Context Headers (Titles)
        if line.startswith("### "):
            current_context = line.replace("### ", "").strip()
            continue

        # 3. Detect Links (Deep Navigation)
        matches = link_pattern.findall(line)
        for anchor_text, url in matches:
            if "cookie" in anchor_text.lower() or "impressum" in anchor_text.lower() or "datenschutz" in anchor_text.lower():
                continue
            
            concept = f"{anchor_text}. {current_context}."
            if "immobilien" in url:
                 concept += " Real Estate Property Search."
            
            success = upsert_point("tara", url, concept, "engelvoelkers.com")
            if success:
                count += 1
                
    print(f"\n🎉 Total Pages Indexed: {count}")

def main():
    print("🚀 Starting Sitemap Ingestion for Engel & Völkers (Universal Schema v1)...")
    
    try:
        requests.get(f"{RAG_SERVICE_URL}/health", timeout=2)
    except:
        print(f"❌ RAG Service likely down at {RAG_SERVICE_URL}. Ensure it is running.")
    
    parse_and_ingest(SOURCE_FILE)

if __name__ == "__main__":
    main()

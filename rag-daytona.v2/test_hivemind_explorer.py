#!/usr/bin/env python3
"""
HiveMind Data Explorer

This script searches your Qdrant HiveMind to show:
1. What domains are indexed
2. What data structure is used
3. Raw chunks from each domain
4. Document types available

USAGE:
    python3 test_hivemind_explorer.py
"""

import os
import sys
import json
from qdrant_client import QdrantClient
from qdrant_client.http import models

# Configuration from environment
QDRANT_URL = os.getenv("QDRANT_URL", "https://878ba705-a90e-44d7-96fc-782399dd97ae.europe-west3-0.gcp.cloud.qdrant.io:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "WAkhOeXiD3DShev81qxn5PYKpQ9t6ufb")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "tara_hive")

print("=" * 80)
print("🧠 HIVE MIND DATA EXPLORER")
print("=" * 80)
print(f"\n📡 Connecting to Qdrant: {QDRANT_URL}")
print(f"📚 Collection: {COLLECTION_NAME}\n")

try:
    # Initialize Qdrant client
    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    
    # Check if collection exists
    collections = client.get_collections().collections
    collection_names = [c.name for c in collections]
    
    print(f"✅ Connected! Found {len(collections)} collection(s):")
    for c in collection_names:
        print(f"   - {c}")
    
    if COLLECTION_NAME not in collection_names:
        print(f"\n❌ Collection '{COLLECTION_NAME}' not found!")
        sys.exit(1)
    
    # Get collection info
    collection_info = client.get_collection(COLLECTION_NAME)
    print(f"\n📊 Collection Info:")
    print(f"   - Vectors: {collection_info.vectors_count:,}")
    print(f"   - Points: {collection_info.points_count:,}")
    print(f"   - Dimensions: {collection_info.config.params.vectors.get('size', 'N/A')}")
    
    # Get all document types
    print("\n" + "=" * 80)
    print("📋 DOCUMENT TYPES IN HIVE MIND")
    print("=" * 80)
    
    # Scroll through all points to find unique doc_types
    doc_types = {}
    domains = {}
    
    scroll_response = client.scroll(
        collection_name=COLLECTION_NAME,
        limit=1000,  # Get up to 1000 points
        with_payload=True,
        with_vectors=False
    )
    
    points = scroll_response[0]
    
    for point in points:
        payload = point.payload or {}
        doc_type = payload.get('doc_type', 'unknown')
        domain = payload.get('domain', 'unknown')
        
        # Count doc types
        if doc_type not in doc_types:
            doc_types[doc_type] = 0
        doc_types[doc_type] += 1
        
        # Count domains
        if domain not in domains:
            domains[domain] = 0
        domains[domain] += 1
    
    print(f"\nFound {len(doc_types)} document type(s):")
    for doc_type, count in sorted(doc_types.items(), key=lambda x: -x[1]):
        print(f"   📄 {doc_type}: {count:,} documents")
    
    print(f"\nFound {len(domains)} domain(s):")
    for domain, count in sorted(domains.items(), key=lambda x: -x[1]):
        print(f"   🌐 {domain}: {count:,} documents")
    
    # Show sample chunks from each doc type
    print("\n" + "=" * 80)
    print("📝 SAMPLE RAW CHUNKS FROM EACH DOCUMENT TYPE")
    print("=" * 80)
    
    for doc_type in doc_types.keys():
        print(f"\n{'='*80}")
        print(f"📄 DOCUMENT TYPE: {doc_type}")
        print(f"{'='*80}")
        
        # Search for this doc type
        search_response = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=3,  # Get 3 samples
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="doc_type",
                        match=models.MatchValue(value=doc_type)
                    )
                ]
            ),
            with_payload=True,
            with_vectors=False
        )
        
        samples = search_response[0]
        
        for i, point in enumerate(samples, 1):
            payload = point.payload or {}
            print(f"\n--- Sample {i} (ID: {point.id}) ---")
            
            # Show key fields
            print(f"🔑 ID: {point.id}")
            print(f"📄 doc_type: {payload.get('doc_type', 'N/A')}")
            print(f"🌐 domain: {payload.get('domain', 'N/A')}")
            
            # Show text content
            text = payload.get('text', '')
            if text:
                print(f"📝 text: {text[:200]}{'...' if len(text) > 200 else ''}")
            
            # Show other common fields
            common_fields = ['url', 'page_type', 'selector', 'element_type', 
                           'zone', 'sequence', 'action', 'entity', 'title']
            
            for field in common_fields:
                if field in payload and payload[field]:
                    value = payload[field]
                    if isinstance(value, list):
                        print(f"📌 {field}: {value[:3]}{'...' if len(value) > 3 else ''}")
                    elif isinstance(value, str) and len(value) > 100:
                        print(f"📌 {field}: {value[:100]}...")
                    else:
                        print(f"📌 {field}: {value}")
            
            # Show full payload for first sample
            if i == 1:
                print(f"\n📦 FULL PAYLOAD (JSON):")
                print(json.dumps(payload, indent=2, default=str)[:1000])
                if len(json.dumps(payload, default=str)) > 1000:
                    print("...(truncated)")
    
    # Search for specific domains
    print("\n" + "=" * 80)
    print("🔍 SEARCH BY DOMAIN")
    print("=" * 80)
    
    for domain in list(domains.keys())[:5]:  # Show top 5 domains
        print(f"\n{'='*80}")
        print(f"🌐 DOMAIN: {domain}")
        print(f"{'='*80}")
        
        search_response = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=2,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="domain",
                        match=models.MatchValue(value=domain)
                    )
                ]
            ),
            with_payload=True,
            with_vectors=False
        )
        
        samples = search_response[0]
        
        for i, point in enumerate(samples, 1):
            payload = point.payload or {}
            print(f"\n--- Sample {i} ---")
            print(f"📄 Type: {payload.get('doc_type', 'N/A')}")
            print(f"📝 Text: {payload.get('text', 'N/A')[:150]}")
            
            # If it's a Website_Map type, show navigation info
            if payload.get('doc_type') == 'Website_Map':
                print(f"🔗 URL: {payload.get('url', 'N/A')}")
                print(f"📋 Page Type: {payload.get('page_type', 'N/A')}")
                if 'navigation_paths' in payload:
                    print(f"🧭 Navigation: {payload['navigation_paths'][:2]}")
    
    print("\n" + "=" * 80)
    print("✅ EXPLORATION COMPLETE")
    print("=" * 80)
    
except Exception as e:
    print(f"\n❌ Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

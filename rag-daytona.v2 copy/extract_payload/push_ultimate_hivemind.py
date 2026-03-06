import os
import json
import uuid
import requests

QDRANT_URL = os.environ.get('QDRANT_URL', 'https://878ba705-a90e-44d7-96fc-782399dd97ae.europe-west3-0.gcp.cloud.qdrant.io:6333')
QDRANT_API_KEY = os.environ.get('QDRANT_API_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJtIn0.ao39iukssxR-CapMCzch2RRRPrEWwotD7hnOZZ2L95Q')
COLLECTION_NAME = "tara_hive"
RAG_SERVICE_URL = os.environ.get('RAG_SERVICE_URL', 'http://localhost:8003')

qdrant_headers = {
    "api-key": QDRANT_API_KEY,
    "Content-Type": "application/json"
}

def get_embedding(text):
    try:
        resp = requests.post(
            f"{RAG_SERVICE_URL}/api/v1/embed",
            json={"text": text},
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json().get("embedding")
    except Exception as e:
        print(f"Embed error: {e}")
    return None

def upsert_batch(points):
    try:
        resp = requests.put(
            f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points",
            headers=qdrant_headers,
            json={"points": points},
            timeout=30
        )
        if resp.status_code == 200:
            return True
        print(f"Qdrant batch error: {resp.text}")
    except Exception as e:
        print(f"Qdrant request error: {e}")
    return False

def main():
    files_to_process = {
        "/Users/amar/demo.davinciai/rag-daytona.v2/extract_payload/groq_extracted_hivemind.json": "groq.com",
        "/Users/amar/demo.davinciai/rag-daytona.v2/extract_payload/console_groq_extracted_hivemind.json": "console.groq.com"
    }
    
    points = []
    total_added = 0
    
    for filepath, default_domain in files_to_process.items():
        print(f"\nProcessing {filepath} for domain {default_domain}...")
        with open(filepath, "r") as infile:
            data = json.load(infile)
            
        for item in data:
            doc_type = item.get("doc_type")
            
            if doc_type == "Website_Map":
                domain = item.get("domain", default_domain)
                text_to_embed = f"Strategy Map for {domain}: {item.get('concept', '')} {item.get('url', '')}"
                
                payload = {
                    "doc_type": "Website_Map", 
                    "type": "map",             # Ultimate Architecture Tag
                    "domain": domain,
                    "url": item.get("url"),
                    "concept": item.get("concept"),
                    "sequence": item.get("sequence", []),
                    "blocking_rules": item.get("blocking_rules", []),
                    "action_script": item.get("action_script")
                }
                
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"map:{domain}:{item.get('url')}"))
                
            elif doc_type == "Visual_Hint":
                domain = item.get("domain", default_domain)
                text_to_embed = f"Visual Hint for {domain}: {item.get('description', '')} - {item.get('text_pattern', '')} {item.get('keyword_match', '')}"
                
                payload = {
                    "doc_type": "Visual_Hint",
                    "type": "hint",            # Ultimate Architecture Tag
                    "domain": domain,
                    "selector": item.get("selector"),
                    "keyword_match": item.get("keyword_match"),
                    "element_type": item.get("element_type"),
                    "text_pattern": item.get("text_pattern"),
                    "zone": item.get("zone"),
                    "description": item.get("description")
                }
                
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hint:{domain}:{item.get('selector')}:{item.get('keyword_match')}"))
                
            else:
                continue
                
            vec = get_embedding(text_to_embed)
            if not vec:
                # If embeddings server is unreachable, we'll gracefully complain, but our mission logic
                # allows zero-vector fallback matching using direct Redis if needed. We'll still need
                # the vector for Qdrant though.
                print(f"  [!] Failed to get embedding for: {text_to_embed}")
                continue
                
            points.append({
                "id": point_id,
                "vector": vec,
                "payload": payload
            })
            
            if len(points) >= 30:
                print(f"  Pushing batch of {len(points)} points...")
                if upsert_batch(points):
                    total_added += len(points)
                points = []
                
    if points:
        print(f"  Pushing final batch of {len(points)} points...")
        if upsert_batch(points):
            total_added += len(points)
            
    print(f"\n🚀 SUCCESS! Uploaded {total_added} 'Ultimate' Hivemind vectors to Qdrant.")
    
if __name__ == "__main__":
    main()

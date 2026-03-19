import requests
import os
import json

QDRANT_URL = 'https://878ba705-a90e-44d7-96fc-782399dd97ae.europe-west3-0.gcp.cloud.qdrant.io:6333'
QDRANT_API_KEY = os.environ.get('QDRANT_API_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJtIn0.ao39iukssxR-CapMCzch2RRRPrEWwotD7hnOZZ2L95Q')
COLLECTION_NAME = "tara_hive"

headers = {
    "api-key": QDRANT_API_KEY,
    "Content-Type": "application/json"
}

def delete_by_doc_type(doc_type):
    payload = {
        "filter": {
            "must": [
                {
                    "key": "doc_type",
                    "match": {
                        "value": doc_type
                    }
                }
            ]
        }
    }
    
    print(f"Deleting all points with doc_type: {doc_type} from {COLLECTION_NAME}...")
    
    try:
        resp = requests.post(
            f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points/delete",
            headers=headers,
            json=payload,
            timeout=30
        )
        if resp.status_code == 200:
            print(f"✓ Success! Result: {resp.json().get('status')}")
        else:
            print(f"✗ Failed ({resp.status_code}): {resp.text}")
    except Exception as e:
        print(f"✗ Error: {e}")

if __name__ == "__main__":
    delete_by_doc_type("Website_Map")
    
    # Since visual hints go with website maps, let's delete those too to completely wipe the mess
    delete_by_doc_type("Visual_Hint")
    
    # Check remaining points
    try:
        resp = requests.get(f"{QDRANT_URL}/collections/{COLLECTION_NAME}", headers=headers)
        if resp.status_code == 200:
            count = resp.json().get('result', {}).get('points_count', '?')
            print(f"\nRemaining points in {COLLECTION_NAME}: {count}")
    except:
        pass

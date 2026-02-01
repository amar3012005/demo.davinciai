#!/usr/bin/env python3
"""
Populate Qdrant Hive Mind with Daytona Support Cases
Uses REAL embeddings from the RAG service for proper semantic search
"""

import requests
import json
from datetime import datetime

QDRANT_URL = "http://qdrant-n80wo80os08gswko4040wo8g.116.202.24.69.sslip.io:6333"
QDRANT_API_KEY = "WAkhOeXiD3DShev81qxn5PYKpQ9t6ufb"
COLLECTION_NAME = "tara_case_memory"
RAG_SERVICE_URL = "https://localhost:8444" 
# Note: verify=False is needed for self-signed certificates in dev/demo environments # Or http://0.0.0.0:8003 since we are on the host

headers = {
    "api-key": QDRANT_API_KEY,
    "Content-Type": "application/json"
}

def get_embedding_from_rag(text):
    """Get real embedding from RAG service using the BGE-M3 model"""
    try:
        # Use the internal embedding endpoint if available, otherwise use a query to get embeddings
        # For now, we'll use a workaround: call the save_case endpoint which generates embeddings
        response = requests.post(
            f"{RAG_SERVICE_URL}/api/v1/embed",
            json={"text": text},
            timeout=30,
            verify=False
        )
        if response.status_code == 200:
            return response.json().get("embedding")
    except Exception as e:
        print(f"  ⚠️ Embedding API not available: {e}")
    return None

def upsert_case_via_rag(user_id, issue, solution, issue_type, customer_segment):
    """Use RAG service's save_case endpoint to properly embed and store"""
    try:
        response = requests.post(
            f"{RAG_SERVICE_URL}/api/v1/save_case",
            json={
                "user_id": user_id,
                "issue": issue,
                "solution": solution,
                "metadata": {
                    "issue_type": issue_type,
                    "customer_segment": customer_segment,
                    "product": "daytona",
                    "timestamp": datetime.now().isoformat()
                }
            },
            timeout=30,
            verify=False
        )
        return response.json()
    except Exception as e:
        return {"error": str(e)}

# Daytona Support Cases
CASES = [
    {
        "user_id": "+15550201",
        "issue": "My Daytona sandbox takes 5+ seconds to create, not the advertised sub-90ms",
        "solution": "Check your API response time vs sandbox init time. Sub-90ms is network + init. If >1s, check: 1) API key valid 2) Region latency (EU/US) 3) Concurrent requests (rate limiting). Use: daytona = Daytona(api_key='key'); start=time.time(); sandbox = daytona.create(CreateSandboxParams(language='python')); print(time.time()-start)",
        "issue_type": "performance_sandbox_creation",
        "customer_segment": "developer"
    },
    {
        "user_id": "+15550202",
        "issue": "How do I install and use Daytona Python SDK correctly?",
        "solution": "Install with: pip install daytona. Usage: from daytona import Daytona, CreateSandboxParams; daytona = Daytona(api_key='YOUR_KEY'); params = CreateSandboxParams(language='python'); sandbox = daytona.create(params); response = sandbox.process.code_run('print(1+1)'); print(response.result); daytona.remove(sandbox)",
        "issue_type": "sdk_installation",
        "customer_segment": "developer"
    },
    {
        "user_id": "+15550203",
        "issue": "How do I upload files to Daytona sandbox?",
        "solution": "Use sandbox.fs API: file_content = b'Hello, World!'; sandbox.fs.upload_file('/home/daytona/data.txt', file_content). For listing files: response = sandbox.process.exec('ls /home/daytona', cwd='/home/daytona', timeout=10); print(response.result)",
        "issue_type": "file_operations",
        "customer_segment": "developer"
    },
    {
        "user_id": "+15550204",
        "issue": "My code runs in Daytona but exit_code is non-zero",
        "solution": "Check response object: response = sandbox.process.code_run('python script.py'); if response.exit_code != 0: print(f'Error: {response.exit_code} {response.result}'). Exit code 1 = execution error, check response.result for details. Ensure Python 3.x compatible code.",
        "issue_type": "code_execution_error",
        "customer_segment": "developer"
    },
    {
        "user_id": "+15550205",
        "issue": "What is Daytona pricing and free tier?",
        "solution": "Daytona offers pay-as-you-go: Compute (vCPU): $0.0504/h or $0.00001400/s; Memory (GiB): $0.0162/h; Storage (GiB): $0.000108/h. Free tier: $200 free compute included, 5GB storage free. See daytona.io/pricing for details.",
        "issue_type": "pricing_inquiry",
        "customer_segment": "startup"
    },
    {
        "user_id": "+15550206",
        "issue": "Can I use docker-compose with Daytona?",
        "solution": "Yes! Daytona supports Docker Compose. Use: params = CreateSandboxParams(language='python'); sandbox = daytona.create(params); sandbox.process.exec('docker compose up -d', cwd='/path/to/compose'). Daytona handles Dockerfile support natively.",
        "issue_type": "docker_support",
        "customer_segment": "devops"
    },
    {
        "user_id": "+15550207",
        "issue": "Do I need to manually delete Daytona sandboxes? Will I be charged?",
        "solution": "Always call daytona.remove(sandbox) after use to stop billing. Sandboxes persist until explicitly removed. Best practice: try: result = sandbox.process.code_run(code) finally: daytona.remove(sandbox). Check Dashboard > Sandboxes to manually delete forgotten ones.",
        "issue_type": "billing_cleanup",
        "customer_segment": "enterprise"
    },
    {
        "user_id": "+15550208",
        "issue": "Which Daytona regions are available and how do I choose?",
        "solution": "Daytona offers: EU Central (Frankfurt), EU West (London), US East (Washington DC), US West (Oregon), India Asia-South. Choose based on your location for lowest latency. Contact sales for region-specific deployment.",
        "issue_type": "regional_deployment",
        "customer_segment": "enterprise"
    },
    {
        "user_id": "+15550209",
        "issue": "Is Daytona HIPAA and SOC 2 compliant? Can I run in single-tenant mode?",
        "solution": "Yes! Daytona meets HIPAA, SOC 2, and GDPR standards out of the box. Enterprise single-tenant available: run Daytona on your own isolated infrastructure. Contact Daytona enterprise sales for compliance documentation.",
        "issue_type": "security_compliance",
        "customer_segment": "enterprise"
    },
    {
        "user_id": "+15550210",
        "issue": "How do I get my Daytona API key and authenticate?",
        "solution": "1) Go to https://daytona.io/dashboard 2) Sign up/login 3) Navigate to API Keys section 4) Click Create New API Key 5) Copy and save securely. In code: from daytona import Daytona; daytona = Daytona(api_key='your_key'). Or set DAYTONA_API_KEY env var.",
        "issue_type": "authentication_setup",
        "customer_segment": "onboarding"
    }
]

def main():
    print("🧪 Populating Qdrant Hive Mind via RAG Service (Real Embeddings)")
    print("=" * 70)
    print(f"RAG Service: {RAG_SERVICE_URL}")
    print(f"Qdrant Collection: {COLLECTION_NAME}")
    
    # Check RAG service health
    print("\n📊 Checking RAG service...")
    try:
        health = requests.get(f"{RAG_SERVICE_URL}/health", timeout=5, verify=False)
        if health.status_code == 200:
            print("✅ RAG service is healthy")
        else:
            print(f"⚠️ RAG service returned {health.status_code}")
    except Exception as e:
        print(f"❌ RAG service not reachable: {e}")
        return
    
    # Upsert cases via RAG save_case endpoint
    print("\n📝 Saving Daytona support cases via RAG service...")
    success_count = 0
    for case in CASES:
        result = upsert_case_via_rag(
            case["user_id"],
            case["issue"],
            case["solution"],
            case["issue_type"],
            case["customer_segment"]
        )
        if result.get("status") == "success" or result.get("success"):
            print(f"  ✅ {case['issue_type']}")
            success_count += 1
        else:
            print(f"  ❌ {case['issue_type']}: {result}")
    
    # Verify count
    print(f"\n📊 Verifying Qdrant collection...")
    try:
        count_resp = requests.post(
            f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points/count",
            headers=headers,
            json={}
        )
        count = count_resp.json().get("result", {}).get("count", 0)
        print(f"  Total points in {COLLECTION_NAME}: {count}")
    except Exception as e:
        print(f"  ⚠️ Could not verify count: {e}")
    
    print("\n" + "=" * 70)
    print("🎉 DAYTONA HIVE MIND POPULATED!")
    print(f"✅ {success_count}/{len(CASES)} cases uploaded with real embeddings")
    print(f"✅ Collection: {COLLECTION_NAME} (384-D Paraphrase-MiniLM)")
    print("=" * 70)

if __name__ == "__main__":
    main()

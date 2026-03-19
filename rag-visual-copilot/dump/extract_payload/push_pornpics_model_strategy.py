"""
push_pornpics_model_strategy.py

Pushes a "find model / pornstar" strategy + visual hints into the Hivemind (Qdrant tara_hive).
This tells TARA that whenever the user asks about a specific model/pornstar on pornpics,
she must ALWAYS navigate to the Pornstars section first and then find the model inside it.

Run:
    python push_pornpics_model_strategy.py

Requirements:
    - RAG service running on localhost:8003 (for embeddings)
    - Qdrant accessible (env: QDRANT_URL / QDRANT_API_KEY)
"""

import os
import uuid
import requests

QDRANT_URL = os.environ.get(
    "QDRANT_URL",
    "https://878ba705-a90e-44d7-96fc-782399dd97ae.europe-west3-0.gcp.cloud.qdrant.io:6333",
)
QDRANT_API_KEY = os.environ.get(
    "QDRANT_API_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJtIn0.ao39iukssxR-CapMCzch2RRRPrEWwotD7hnOZZ2L95Q",
)
COLLECTION_NAME = "tara_hive"
RAG_SERVICE_URL = os.environ.get("RAG_SERVICE_URL", "http://localhost:8003")

qdrant_headers = {
    "api-key": QDRANT_API_KEY,
    "Content-Type": "application/json",
}


def get_embedding(text: str):
    try:
        resp = requests.post(
            f"{RAG_SERVICE_URL}/api/v1/embed",
            json={"text": text},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json().get("embedding")
        print(f"  [!] Embed HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"  [!] Embed error: {e}")
    return None


def upsert_points(points: list) -> bool:
    try:
        resp = requests.put(
            f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points",
            headers=qdrant_headers,
            json={"points": points},
            timeout=30,
        )
        if resp.status_code == 200:
            return True
        print(f"  [!] Qdrant error: {resp.text[:300]}")
    except Exception as e:
        print(f"  [!] Qdrant request error: {e}")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# DATA: Strategy + Hints to push
# ─────────────────────────────────────────────────────────────────────────────

ENTRIES = [
    # ── Strategy_Sequence (pornpics.de) ──────────────────────────────────────
    # This is the MASTER ROUTE the Pre-Decision Gate will return when the user
    # asks "show me <model name>" or "find <model name>" on pornpics.de.
    {
        "doc_type": "Strategy_Sequence",
        "domain": "pornpics.de",
        "action": "extraction",
        "sequence": [
            "Click Pornstars",
            "LAST_MILE: Find the model gallery inside Pornstars section",
        ],
        "constraints_order": ["entity"],
        "blocking_rules": {"open_gallery": ["entity"]},
        "example_url": "https://www.pornpics.de/pornstars/",
        "text": (
            "Strategy for finding a specific model on pornpics.de: "
            "Always click the Pornstars nav link first, then use Last-Mile to find and open the model's page. "
            "URL: https://www.pornpics.de/pornstars/"
        ),
        "concept": "Navigate to Pornstars section to find a specific model or pornstar",
    },

    # ── Strategy_Sequence (pornpics.com) ─────────────────────────────────────
    {
        "doc_type": "Strategy_Sequence",
        "domain": "pornpics.com",
        "action": "extraction",
        "sequence": [
            "Click Pornstars",
            "LAST_MILE: Find the model gallery inside Pornstars section",
        ],
        "constraints_order": ["entity"],
        "blocking_rules": {"open_gallery": ["entity"]},
        "example_url": "https://www.pornpics.com/pornstars/",
        "text": (
            "Strategy for finding a specific model on pornpics.com: "
            "Always click the Pornstars nav link first, then use Last-Mile to find and open the model's page. "
            "URL: https://www.pornpics.com/pornstars/"
        ),
        "concept": "Navigate to Pornstars section to find a specific model or pornstar",
    },

    # ── Visual_Hint: Pornstars nav link (pornpics.de) ────────────────────────
    # HIGH-PRIORITY hint: any model/pornstar search should trigger this link.
    {
        "doc_type": "Visual_Hint",
        "domain": "pornpics.de",
        "selector": "a[href*='/pornstars/']",
        "keyword_match": "find model show pornstar actress performer",
        "element_type": "a",
        "text_pattern": "Pornstars",
        "zone": "nav",
        "description": (
            "Primary pornstar/model lookup entry point. "
            "Click 'Pornstars' nav link to reach https://www.pornpics.de/pornstars/ "
            "when the user asks about a specific model or performer."
        ),
    },

    # ── Visual_Hint: Pornstars nav link (pornpics.com) ───────────────────────
    {
        "doc_type": "Visual_Hint",
        "domain": "pornpics.com",
        "selector": "a[href*='/pornstars/']",
        "keyword_match": "find model show pornstar actress performer",
        "element_type": "a",
        "text_pattern": "Pornstars",
        "zone": "nav",
        "description": (
            "Primary pornstar/model lookup entry point. "
            "Click 'Pornstars' nav link to reach https://www.pornpics.com/pornstars/ "
            "when the user asks about a specific model or performer."
        ),
    },

    # ── Visual_Hint: Model search input inside Pornstars (pornpics.de) ───────
    {
        "doc_type": "Visual_Hint",
        "domain": "pornpics.de",
        "selector": "input[name='q'], input[placeholder*='Search'], input[type='search']",
        "keyword_match": "search model pornstar name",
        "element_type": "input",
        "text_pattern": "Search Pornstars",
        "zone": "main",
        "description": (
            "Search input on the Pornstars listing page. "
            "Use this to type the model name and find her dedicated gallery page. "
            "URL context: https://www.pornpics.de/pornstars/"
        ),
    },

    # ── Visual_Hint: Model search input inside Pornstars (pornpics.com) ──────
    {
        "doc_type": "Visual_Hint",
        "domain": "pornpics.com",
        "selector": "input[name='q'], input[placeholder*='Search'], input[type='search']",
        "keyword_match": "search model pornstar name",
        "element_type": "input",
        "text_pattern": "Search Pornstars",
        "zone": "main",
        "description": (
            "Search input on the Pornstars listing page. "
            "Use this to type the model name and find her dedicated gallery page. "
            "URL context: https://www.pornpics.com/pornstars/"
        ),
    },
]


def _make_point_id(doc_type: str, domain: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_type}:{domain}:{key}"))


def build_payload(entry: dict, default_domain: str = "pornpics.de"):
    doc_type = entry.get("doc_type", "")
    domain = entry.get("domain", default_domain)

    if doc_type == "Strategy_Sequence":
        text_to_embed = (
            f"Strategy for {domain}: {entry.get('concept', '')} "
            f"{entry.get('text', '')} {entry.get('example_url', '')}"
        )
        payload = {
            "doc_type": "Strategy_Sequence",
            "type": "strategy",
            "domain": domain,
            "action": entry.get("action"),
            "sequence": entry.get("sequence", []),
            "constraints_order": entry.get("constraints_order", []),
            "blocking_rules": entry.get("blocking_rules", {}),
            "example_url": entry.get("example_url"),
            "text": entry.get("text"),
            "concept": entry.get("concept"),
        }
        point_id = _make_point_id(doc_type, domain, entry.get("concept", ""))

    elif doc_type == "Visual_Hint":
        text_to_embed = (
            f"Visual Hint for {domain}: {entry.get('description', '')} "
            f"- {entry.get('text_pattern', '')} {entry.get('keyword_match', '')}"
        )
        payload = {
            "doc_type": "Visual_Hint",
            "type": "hint",
            "domain": domain,
            "selector": entry.get("selector"),
            "keyword_match": entry.get("keyword_match"),
            "element_type": entry.get("element_type"),
            "text_pattern": entry.get("text_pattern"),
            "zone": entry.get("zone"),
            "description": entry.get("description"),
        }
        point_id = _make_point_id(
            doc_type, domain,
            f"{entry.get('selector', '')}:{entry.get('keyword_match', '')}",
        )

    elif doc_type == "Website_Map":
        text_to_embed = f"Strategy Map for {domain}: {entry.get('concept', '')} {entry.get('url', '')}"
        payload = {
            "doc_type": "Website_Map",
            "type": "map",
            "domain": domain,
            "url": entry.get("url"),
            "concept": entry.get("concept"),
            "sequence": entry.get("sequence", []),
            "blocking_rules": entry.get("blocking_rules", []),
            "action_script": entry.get("action_script"),
        }
        point_id = _make_point_id(doc_type, domain, entry.get("url", ""))
    else:
        return None, None, None

    return point_id, text_to_embed, payload


def main():
    print("🚀 Pushing pornpics model-lookup strategy + visual hints to Hivemind…\n")
    points = []
    success = 0

    for entry in ENTRIES:
        doc_type = entry.get("doc_type", "?")
        domain = entry.get("domain", "?")
        label = entry.get("concept") or entry.get("text_pattern") or entry.get("action", "?")
        print(f"  ⏳ [{doc_type}] {domain} | {label}")

        point_id, text_to_embed, payload = build_payload(entry)
        if not point_id:
            print(f"     [!] Skipped unknown doc_type")
            continue

        vec = get_embedding(text_to_embed)
        if not vec:
            print(f"     [!] Embedding failed — skipping")
            continue

        points.append({"id": point_id, "vector": vec, "payload": payload})
        print(f"     ✅ Embedded (id={point_id[:8]}…)")

    if not points:
        print("\n❌ Nothing to push. Check RAG service is running on", RAG_SERVICE_URL)
        return

    print(f"\n  Pushing {len(points)} points to Qdrant…")
    if upsert_points(points):
        success = len(points)
        print(f"\n✅ SUCCESS — {success}/{len(ENTRIES)} entries pushed to Hivemind.")
        print("   Restart the RAG service to invalidate hive cache.")
    else:
        print("\n❌ Qdrant upsert failed. Check QDRANT_URL / QDRANT_API_KEY.")


if __name__ == "__main__":
    main()

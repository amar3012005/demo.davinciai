"""
Seed tara_hive collection with Groq console navigation data.
Uses the same ONNX embeddings as the running RAG service (384-D).

Usage:
    python seed_hive.py
"""

import os
import sys
import uuid
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Qdrant config
QDRANT_URL = os.environ.get("QDRANT_URL", "https://878ba705-a90e-44d7-96fc-782399dd97ae.europe-west3-0.gcp.cloud.qdrant.io:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJtIn0.ao39iukssxR-CapMCzch2RRRPrEWwotD7hnOZZ2L95Q")
COLLECTION = "tara_hive"

# ── Seed Data ──────────────────────────────────────────────────

STRATEGIES = [
    {
        "domain": "console.groq.com",
        "action": "extraction",
        "sequence": ["Click Dashboard in sidebar", "Navigate to Usage tab", "Read token usage stats"],
        "constraints_order": ["date_range"],
        "blocking_rules": {},
        "example_url": "https://console.groq.com/dashboard/usage",
        "text": "Strategy for extraction on console.groq.com: Click Dashboard in sidebar → Navigate to Usage tab → Read token usage stats"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click API Keys in sidebar navigation", "View list of API keys"],
        "constraints_order": [],
        "blocking_rules": {},
        "example_url": "https://console.groq.com/keys",
        "text": "Strategy for navigation to API Keys on console.groq.com: Click API Keys in sidebar → View list of API keys"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click Playground in sidebar", "Select model from dropdown", "Type prompt in input area"],
        "constraints_order": ["model_selection"],
        "blocking_rules": {},
        "example_url": "https://console.groq.com/playground",
        "text": "Strategy for navigation to Playground on console.groq.com: Click Playground → Select model → Type prompt"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click Docs link in top navigation or sidebar", "Browse documentation pages"],
        "constraints_order": [],
        "blocking_rules": {},
        "example_url": "https://console.groq.com/docs",
        "text": "Strategy for navigation to Docs on console.groq.com: Click Docs link in top navigation or sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "extraction",
        "sequence": ["Click Dashboard in sidebar", "Look at spend limits section", "Read current spend and limits"],
        "constraints_order": ["date_range"],
        "blocking_rules": {},
        "example_url": "https://console.groq.com/dashboard",
        "text": "Strategy for extraction of spend limits on console.groq.com: Click Dashboard → Look at spend limits → Read data"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click Settings or Account in sidebar", "Navigate to settings page"],
        "constraints_order": [],
        "blocking_rules": {},
        "example_url": "https://console.groq.com/settings",
        "text": "Strategy for navigation to Settings on console.groq.com: Click Settings or Account in sidebar"
    },
    # ── Deep Link Strategies ─────────────────────────────────────
    # These solve the "Deep Link Trap" where docs sub-pages are
    # hidden behind the Docs sidebar and not visible from /home.
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate Prompt Caching in the left sidebar", "Click Prompt Caching"],
        "constraints_order": [],
        "blocking_rules": {"Click Prompt Caching": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/prompt-caching",
        "text": "Strategy for navigation to Prompt Caching documentation on console.groq.com: Click Docs in top nav → Locate Prompt Caching in sidebar → Click Prompt Caching"
    },
    # ── Extraction variants (for "what is X?" queries) ───────────
    # Mind Reader classifies "what is prompt caching" as extraction,
    # not navigation. Without these, the only extraction strategy is
    # Dashboard → Usage, which causes all "what is X" to hallucinate.
    {
        "domain": "console.groq.com",
        "action": "extraction",
        "sequence": ["Click the Docs link in the top navigation", "Click Prompt Caching in the left sidebar"],
        "constraints_order": [],
        "blocking_rules": {"Click Prompt Caching in the left sidebar": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/prompt-caching",
        "text": "Strategy for extraction of prompt caching information on console.groq.com: Click Docs in top nav → Click Prompt Caching in the left sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "extraction",
        "sequence": ["Click the Docs link in the top navigation", "Click Coding with Groq in the left sidebar", "Click Kilo Code in the sub-navigation"],
        "constraints_order": [],
        "blocking_rules": {"Click Kilo Code in the sub-navigation": ["Click Coding with Groq in the left sidebar"]},
        "example_url": "https://console.groq.com/docs/coding-with-groq/kilo-code",
        "text": "Strategy for extraction of Kilo Code documentation on console.groq.com: Click Docs → Click Coding with Groq → Click Kilo Code"
    },
    {
        "domain": "console.groq.com",
        "action": "extraction",
        "sequence": ["Click the Docs link in the top navigation", "Click Tool Use in the left sidebar"],
        "constraints_order": [],
        "blocking_rules": {"Click Tool Use in the left sidebar": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/tool-use",
        "text": "Strategy for extraction of tool use documentation on console.groq.com: Click Docs in top nav → Click Tool Use in the left sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "extraction",
        "sequence": ["Click the Docs link in the top navigation", "Click Structured Outputs in the left sidebar"],
        "constraints_order": [],
        "blocking_rules": {"Click Structured Outputs in the left sidebar": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/structured-outputs",
        "text": "Strategy for extraction of structured outputs documentation on console.groq.com: Click Docs in top nav → Click Structured Outputs in the left sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "extraction",
        "sequence": ["Click the Docs link in the top navigation", "Click Reasoning in the left sidebar"],
        "constraints_order": [],
        "blocking_rules": {"Click Reasoning in the left sidebar": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/reasoning",
        "text": "Strategy for extraction of reasoning documentation on console.groq.com: Click Docs in top nav → Click Reasoning in the left sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "extraction",
        "sequence": ["Click the Docs link in the top navigation", "Click Models in the left sidebar"],
        "constraints_order": [],
        "blocking_rules": {"Click Models in the left sidebar": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/models",
        "text": "Strategy for extraction of models documentation on console.groq.com: Click Docs in top nav → Click Models in the left sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "extraction",
        "sequence": ["Click the Docs link in the top navigation", "Click Rate Limits in the left sidebar"],
        "constraints_order": [],
        "blocking_rules": {"Click Rate Limits in the left sidebar": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/rate-limits",
        "text": "Strategy for extraction of rate limits documentation on console.groq.com: Click Docs in top nav → Click Rate Limits in the left sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate Coding with Groq in the left sidebar", "Click Coding with Groq"],
        "constraints_order": [],
        "blocking_rules": {"Click Coding with Groq": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/coding-with-groq",
        "text": "Strategy for navigation to Coding with Groq documentation on console.groq.com: Click Docs in top nav → Locate Coding with Groq in sidebar → Click Coding with Groq"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate Coding with Groq in the left sidebar", "Click Coding with Groq", "Click Kilo Code in the sub-navigation"],
        "constraints_order": [],
        "blocking_rules": {"Click Kilo Code in the sub-navigation": ["Click Coding with Groq"]},
        "example_url": "https://console.groq.com/docs/coding-with-groq/kilo-code",
        "text": "Strategy for navigation to Kilo Code documentation on console.groq.com: Click Docs → Click Coding with Groq → Click Kilo Code in sub-navigation"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate Tool Use in the left sidebar", "Click Tool Use"],
        "constraints_order": [],
        "blocking_rules": {"Click Tool Use": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/tool-use",
        "text": "Strategy for navigation to Tool Use documentation on console.groq.com: Click Docs in top nav → Locate Tool Use in sidebar → Click Tool Use"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate Structured Outputs in the left sidebar", "Click Structured Outputs"],
        "constraints_order": [],
        "blocking_rules": {"Click Structured Outputs": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/structured-outputs",
        "text": "Strategy for navigation to Structured Outputs documentation on console.groq.com: Click Docs in top nav → Locate Structured Outputs in sidebar → Click Structured Outputs"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate Content Moderation in the left sidebar", "Click Content Moderation"],
        "constraints_order": [],
        "blocking_rules": {"Click Content Moderation": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/content-moderation",
        "text": "Strategy for navigation to Content Moderation documentation on console.groq.com: Click Docs in top nav → Click Content Moderation in sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate Text Generation in the left sidebar", "Click Text Generation"],
        "constraints_order": [],
        "blocking_rules": {"Click Text Generation": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/text-generation",
        "text": "Strategy for navigation to Text Generation documentation on console.groq.com: Click Docs in top nav → Click Text Generation in sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate Speech to Text in the left sidebar", "Click Speech to Text"],
        "constraints_order": [],
        "blocking_rules": {"Click Speech to Text": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/speech-to-text",
        "text": "Strategy for navigation to Speech to Text documentation on console.groq.com: Click Docs in top nav → Click Speech to Text in sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate Text to Speech in the left sidebar", "Click Text to Speech"],
        "constraints_order": [],
        "blocking_rules": {"Click Text to Speech": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/text-to-speech",
        "text": "Strategy for navigation to Text to Speech documentation on console.groq.com: Click Docs in top nav → Click Text to Speech in sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate Reasoning in the left sidebar", "Click Reasoning"],
        "constraints_order": [],
        "blocking_rules": {"Click Reasoning": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/reasoning",
        "text": "Strategy for navigation to Reasoning documentation on console.groq.com: Click Docs in top nav → Click Reasoning in sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate Integrations Catalog in the left sidebar", "Click Integrations Catalog"],
        "constraints_order": [],
        "blocking_rules": {"Click Integrations Catalog": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/integrations",
        "text": "Strategy for navigation to Integrations Catalog documentation on console.groq.com: Click Docs in top nav → Click Integrations Catalog in sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate Batch Processing in the left sidebar", "Click Batch Processing"],
        "constraints_order": [],
        "blocking_rules": {"Click Batch Processing": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/batch-processing",
        "text": "Strategy for navigation to Batch Processing documentation on console.groq.com: Click Docs in top nav → Click Batch Processing in sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate OpenAI Compatibility in the left sidebar", "Click OpenAI Compatibility"],
        "constraints_order": [],
        "blocking_rules": {"Click OpenAI Compatibility": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/openai",
        "text": "Strategy for navigation to OpenAI Compatibility documentation on console.groq.com: Click Docs in top nav → Click OpenAI Compatibility in sidebar"
    },
    {
        "domain": "console.groq.com",
        "action": "navigation",
        "sequence": ["Click the Docs link in the top navigation", "Locate LoRA Inference in the left sidebar", "Click LoRA Inference"],
        "constraints_order": [],
        "blocking_rules": {"Click LoRA Inference": ["Click the Docs link in the top navigation"]},
        "example_url": "https://console.groq.com/docs/lora",
        "text": "Strategy for navigation to LoRA Inference documentation on console.groq.com: Click Docs in top nav → Click LoRA Inference in sidebar"
    },
]

VISUAL_HINTS = [
    # Sidebar navigation
    {"domain": "console.groq.com", "entity": "Dashboard link", "selector": "Dashboard", "element_type": "link", "zone": "sidebar", "text_pattern": "Dashboard"},
    {"domain": "console.groq.com", "entity": "API Keys link", "selector": "API Keys", "element_type": "link", "zone": "sidebar", "text_pattern": "API Keys"},
    {"domain": "console.groq.com", "entity": "Playground link", "selector": "Playground", "element_type": "link", "zone": "sidebar", "text_pattern": "Playground"},
    {"domain": "console.groq.com", "entity": "Docs link", "selector": "Docs", "element_type": "link", "zone": "nav", "text_pattern": "Docs"},
    {"domain": "console.groq.com", "entity": "Settings link", "selector": "Settings", "element_type": "link", "zone": "sidebar", "text_pattern": "Settings"},
    # Main content elements
    {"domain": "console.groq.com", "entity": "Usage chart", "selector": "usage-chart", "element_type": "div", "zone": "main", "text_pattern": "Usage"},
    {"domain": "console.groq.com", "entity": "Spend limits", "selector": "spend-limits", "element_type": "div", "zone": "main", "text_pattern": "Spend Limits"},
    {"domain": "console.groq.com", "entity": "API key table", "selector": "api-key-table", "element_type": "table", "zone": "main", "text_pattern": "API Key"},
    {"domain": "console.groq.com", "entity": "Search bar", "selector": "Search", "element_type": "button", "zone": "nav", "text_pattern": "Search"},
    {"domain": "console.groq.com", "entity": "Log In button", "selector": "Log In", "element_type": "link", "zone": "nav", "text_pattern": "Log In"},
    # Docs navigation
    {"domain": "console.groq.com", "entity": "Models page link", "selector": "Models", "element_type": "link", "zone": "sidebar", "text_pattern": "Models"},
    {"domain": "console.groq.com", "entity": "Quickstart link", "selector": "Quickstart", "element_type": "link", "zone": "sidebar", "text_pattern": "Quickstart"},
    {"domain": "console.groq.com", "entity": "Rate Limits link", "selector": "Rate Limits", "element_type": "link", "zone": "sidebar", "text_pattern": "Rate Limits"},
    {"domain": "console.groq.com", "entity": "Reasoning page", "selector": "Reasoning", "element_type": "link", "zone": "sidebar", "text_pattern": "Reasoning"},
    {"domain": "console.groq.com", "entity": "Text Generation link", "selector": "Text Generation", "element_type": "link", "zone": "sidebar", "text_pattern": "Text Generation"},
    {"domain": "console.groq.com", "entity": "Speech to Text link", "selector": "Speech to Text", "element_type": "link", "zone": "sidebar", "text_pattern": "Speech to Text"},
    {"domain": "console.groq.com", "entity": "Tool Use link", "selector": "Tool Use", "element_type": "link", "zone": "sidebar", "text_pattern": "Tool Use"},
]

WEBSITE_MAPS = [
    {
        "domain": "console.groq.com",
        "url": "/",
        "page_type": "landing",
        "text": "Groq console landing page with sidebar navigation: Playground, API Keys, Dashboard, Docs, Models, Settings. Top nav has Search and Log In.",
        "key_elements": ["sidebar-nav", "search-bar", "login-button"],
        "navigation_paths": [
            {"from": "/", "to": "/playground", "element": "Playground sidebar link"},
            {"from": "/", "to": "/keys", "element": "API Keys sidebar link"},
            {"from": "/", "to": "/dashboard", "element": "Dashboard sidebar link"},
            {"from": "/", "to": "/docs", "element": "Docs link"},
        ]
    },
    {
        "domain": "console.groq.com",
        "url": "/dashboard/usage",
        "page_type": "dashboard",
        "text": "Usage Dashboard showing token usage stats, API call counts, spend limits, and billing information. Contains usage charts and data tables.",
        "key_elements": ["usage-chart", "spend-limits", "api-calls-table"],
        "navigation_paths": [
            {"from": "/dashboard", "to": "/dashboard/usage", "element": "Usage tab"},
            {"from": "/dashboard", "to": "/dashboard/billing", "element": "Billing tab"},
        ]
    },
    {
        "domain": "console.groq.com",
        "url": "/keys",
        "page_type": "settings",
        "text": "API Keys management page. Create, view, and delete API keys. Shows key name, prefix, and creation date.",
        "key_elements": ["api-key-table", "create-key-button"],
        "navigation_paths": [
            {"from": "/keys", "to": "/keys/create", "element": "Create API Key button"},
        ]
    },
    {
        "domain": "console.groq.com",
        "url": "/playground",
        "page_type": "tool",
        "text": "Groq Playground for testing LLM models. Select model, write prompt, adjust parameters (temperature, max tokens), and see responses.",
        "key_elements": ["model-selector", "prompt-input", "send-button", "response-area"],
        "navigation_paths": []
    },
    {
        "domain": "console.groq.com",
        "url": "/docs",
        "page_type": "documentation",
        "text": "Groq API documentation with sidebar navigation: Overview, Quickstart, Models, OpenAI Compatibility, Rate Limits, Templates, Text Generation, Speech to Text, Reasoning, Tool Use, Structured Outputs.",
        "key_elements": ["docs-sidebar", "docs-content", "search-docs"],
        "navigation_paths": [
            {"from": "/docs", "to": "/docs/quickstart", "element": "Quickstart link"},
            {"from": "/docs", "to": "/docs/models", "element": "Models link"},
            {"from": "/docs", "to": "/docs/rate-limits", "element": "Rate Limits link"},
        ]
    },
    {
        "domain": "console.groq.com",
        "url": "/docs/reasoning",
        "page_type": "documentation",
        "text": "Reasoning models documentation. Supported models: openai/gpt-oss-20b, openai/gpt-oss-120b, qwen/qwen3-32b. Reasoning format options: parsed, raw, hidden. Quick start code examples.",
        "key_elements": ["models-table", "code-examples", "reasoning-format-options"],
        "navigation_paths": []
    },
]


def main():
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct

    # Load embeddings
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    from optimized_embeddings import OptimizedEmbeddings
    emb = OptimizedEmbeddings("Xenova/paraphrase-multilingual-MiniLM-L12-v2")

    # Connect to Qdrant
    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    info = client.get_collection(COLLECTION)
    logger.info(f"Connected to {COLLECTION}: {info.points_count} existing points")

    points = []

    # Strategies
    for s in STRATEGIES:
        text = s["text"]
        vector = emb.embed_query(text)
        payload = {
            "doc_type": "Strategy_Sequence",
            "domain": s["domain"],
            "action": s["action"],
            "sequence": s["sequence"],
            "constraints_order": s["constraints_order"],
            "blocking_rules": s["blocking_rules"],
            "example_url": s["example_url"],
            "text": text,
        }
        points.append(PointStruct(id=str(uuid.uuid4()), vector=vector, payload=payload))
        logger.info(f"  + Strategy: {s['action']} → {s['example_url']}")

    # Visual Hints
    for h in VISUAL_HINTS:
        text = f"{h['entity']} on {h['domain']}: {h['text_pattern']} ({h['element_type']} in {h['zone']})"
        vector = emb.embed_query(text)
        payload = {
            "doc_type": "Visual_Hint",
            "domain": h["domain"],
            "entity": h["entity"],
            "selector": h["selector"],
            "element_type": h["element_type"],
            "zone": h["zone"],
            "text_pattern": h["text_pattern"],
            "text": text,
        }
        points.append(PointStruct(id=str(uuid.uuid4()), vector=vector, payload=payload))

    logger.info(f"  + {len(VISUAL_HINTS)} Visual Hints")

    # Website Maps
    for m in WEBSITE_MAPS:
        text = m["text"]
        vector = emb.embed_query(text)
        payload = {
            "doc_type": "Website_Map",
            "domain": m["domain"],
            "url": m["url"],
            "page_type": m["page_type"],
            "key_elements": m["key_elements"],
            "navigation_paths": m["navigation_paths"],
            "text": text,
        }
        points.append(PointStruct(id=str(uuid.uuid4()), vector=vector, payload=payload))
        logger.info(f"  + Map: {m['url']} ({m['page_type']})")

    # Upsert all points
    logger.info(f"\nUpserting {len(points)} points to {COLLECTION}...")
    client.upsert(collection_name=COLLECTION, points=points)

    # Verify
    info = client.get_collection(COLLECTION)
    logger.info(f"Done! {COLLECTION} now has {info.points_count} points")


if __name__ == "__main__":
    main()

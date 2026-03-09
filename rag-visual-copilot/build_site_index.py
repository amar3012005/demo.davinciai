#!/usr/bin/env python3
"""
build_site_index.py  —  PageIndex Site Indexer

PURPOSE:
  Parse a structured markdown document (e.g., groq.md) into a hierarchical
  JSON tree ("site_map.json") that enables vectorless top-down reasoning
  for web navigation agents.

USAGE:
  python build_site_index.py \
      --input /path/to/groq.md \
      --output site_map.json \
      [--use-llm]              # Enrich summaries via openai/gpt-oss-20b
      [--domain console.groq.com]

SCHEMA PER NODE:
  {
    "node_id":              str  — unique slug (e.g., "docs_prompt_caching")
    "title":                str  — human-readable section title
    "logical_path":         str  — dotted path from root (e.g., "root.console.docs.prompt_caching")
    "url":                  str  — canonical URL for this section
    "path_regex":           str  — regex to match current URL to this node
    "summary_of_contents":  str  — concise description of what this section contains
    "expected_controls":    list — UI controls expected on this page
    "required_controls":    list — controls needed to interact (date picker, filter, etc.)
    "terminal_capabilities":list — what data can be read/extracted from leaf nodes
    "children":             list — child nodes (recursive)
  }

OUTPUT:
  A single JSON file with:
  {
    "site_metadata": { "domain": "...", "version": "1.0.0", "generated_at": "..." },
    "root": { ... recursive node tree ... }
  }
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("build_site_index")


# ═══════════════════════════════════════════════════════════════════════
# Data Model
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class SiteNode:
    node_id: str
    title: str
    logical_path: str
    url: str = ""
    path_regex: str = ""
    summary_of_contents: str = ""
    expected_controls: List[str] = field(default_factory=list)
    required_controls: List[str] = field(default_factory=list)
    terminal_capabilities: List[str] = field(default_factory=list)
    children: List["SiteNode"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "node_id": self.node_id,
            "title": self.title,
            "logical_path": self.logical_path,
            "url": self.url,
            "path_regex": self.path_regex,
            "summary_of_contents": self.summary_of_contents,
            "expected_controls": self.expected_controls,
            "required_controls": self.required_controls,
            "terminal_capabilities": self.terminal_capabilities,
        }
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        else:
            d["children"] = []
        return d


# ═══════════════════════════════════════════════════════════════════════
# Markdown Parsing — Extract PILLAR sections from groq.md
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class PillarSection:
    """One PILLAR from groq.md."""
    pillar_num: int
    url: str
    title: str
    content: str


def parse_pillars(md_text: str) -> List[PillarSection]:
    """Split the monolithic groq.md into individual PILLAR sections."""
    pattern = re.compile(
        r"^## PILLAR (\d+):\s*(https?://\S+)\s*$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(md_text))
    sections: List[PillarSection] = []

    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        block = md_text[start:end].strip()

        # Extract title
        title_match = re.search(r"^Title:\s*(.+)$", block, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else f"Pillar {m.group(1)}"

        sections.append(PillarSection(
            pillar_num=int(m.group(1)),
            url=m.group(2).strip(),
            title=title,
            content=block,
        ))

    return sections


# ═══════════════════════════════════════════════════════════════════════
# Navigation Structure Extraction
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class NavLink:
    """A link extracted from the sidebar/nav structure."""
    label: str
    url: str
    section: str  # parent section heading (e.g., "Getting Started", "Core Features")


def extract_sidebar_nav(content: str) -> List[NavLink]:
    """
    Extract the documentation sidebar navigation structure from pillar content.
    Looks for the repeated sidebar pattern in console.groq.com docs pages.
    """
    links: List[NavLink] = []
    seen_urls = set()
    current_section = "General"

    for line in content.split("\n"):
        line = line.strip()
        # Section heading: ### Getting Started
        sec_match = re.match(r"^###\s+(.+)$", line)
        if sec_match:
            current_section = sec_match.group(1).strip()
            continue

        # Link: [Label](URL)
        link_matches = re.findall(r"\[([^\]]+)\]\((https://[^)]+)\)", line)
        for label, url in link_matches:
            label = label.strip()
            url = url.strip()
            # Skip image links, tracking pixels, etc.
            if label.startswith("Image") or "cdn.sanity.io" in url or "analytics" in url:
                continue
            if url not in seen_urls:
                seen_urls.add(url)
                links.append(NavLink(label=label, url=url, section=current_section))

    return links


def _slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s_-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:60].strip("_")


def _url_to_path_regex(url: str) -> str:
    """Convert a URL to a regex pattern for matching."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    # Escape special regex chars, then allow trailing slash
    escaped = re.escape(path)
    return f"^{escaped}/?$"


def _extract_page_controls_from_content(content: str, url: str) -> Tuple[List[str], List[str], List[str]]:
    """
    Heuristically extract expected controls, required controls,
    and terminal capabilities from page content.
    """
    expected = []
    required = []
    terminal = []

    content_lower = content.lower()

    # Console pages
    if "console.groq.com" in url:
        # Top nav for console pages
        for ctrl in ["Playground", "API Keys", "Dashboard", "Docs", "Settings"]:
            if ctrl.lower() in content_lower:
                expected.append(ctrl)

        # Sidebar items visible
        if "search" in content_lower:
            expected.append("Search")

    # Detect interactive controls from content
    control_indicators = {
        "date picker": ("Date Picker", True),
        "model filter": ("Model Filter", True),
        "filter": ("Filter", False),
        "dropdown": ("Dropdown", False),
        "search": ("Search Input", False),
        "tab": ("Tab Switcher", False),
        "code execution": ("Code Execution", False),
        "playground": ("Playground Interface", False),
    }
    for keyword, (ctrl_name, is_required) in control_indicators.items():
        if keyword in content_lower and ctrl_name not in expected:
            expected.append(ctrl_name)
            if is_required:
                required.append(ctrl_name)

    # Terminal capabilities (what data can be read)
    if "/docs/" in url:
        terminal.append("read_documentation")
    if "models" in url:
        terminal.extend(["read_model_list", "compare_models", "view_pricing"])
    if "api-reference" in url:
        terminal.extend(["read_api_spec", "view_endpoints"])
    if "rate-limits" in url:
        terminal.extend(["read_rate_limits", "compare_tiers"])
    if "vision" in url:
        terminal.extend(["read_vision_docs", "view_code_examples"])
    if "quickstart" in url:
        terminal.extend(["read_quickstart", "view_setup_steps"])
    if "pricing" in url or "spend" in url:
        terminal.extend(["read_pricing", "view_costs"])
    if "playground" in url:
        terminal.extend(["run_model_inference", "test_prompts"])
    if "keys" in url:
        terminal.extend(["manage_api_keys", "create_key", "view_keys"])
    if "dashboard" in url:
        terminal.extend(["view_metrics", "read_usage_data"])
    if "prompt-caching" in url:
        terminal.extend(["read_prompt_caching_docs"])
    if "privacy" in url:
        terminal.extend(["read_privacy_policy"])
    if "lpu" in url:
        terminal.extend(["read_lpu_architecture"])

    return expected, required, terminal


# ═══════════════════════════════════════════════════════════════════════
# LLM Enrichment  —  Use gpt-oss-20b to generate accurate summaries
# ═══════════════════════════════════════════════════════════════════════

def _llm_enrich_summary(title: str, url: str, content_snippet: str) -> str:
    """
    Use openai/gpt-oss-20b via Groq API to generate a concise,
    navigation-oriented summary for a site node.
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.warning("GROQ_API_KEY not set; skipping LLM enrichment")
        return ""

    try:
        import httpx

        prompt = f"""You are a site-map indexer. Given a page title and content snippet,
write a one-sentence summary that describes what a user can DO or FIND on this page.
Focus on navigation intent — what questions/goals would this page answer?

Page Title: {title}
URL: {url}
Content (first 2000 chars):
{content_snippet[:2000]}

Summary (one sentence, navigation-focused):"""

        payload = {
            "model": "openai/gpt-oss-20b",
            "messages": [
                {"role": "system", "content": "You are a precise site indexer. Respond with only the summary sentence."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_completion_tokens": 100,
        }

        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"LLM enrichment failed for '{title}': {e}")
        return ""


# ═══════════════════════════════════════════════════════════════════════
# Deterministic Summary Generation (fallback / default)
# ═══════════════════════════════════════════════════════════════════════

def _deterministic_summary(title: str, url: str, content: str) -> str:
    """Generate a deterministic navigation-oriented summary without LLM."""
    title_clean = title.replace(" - GroqDocs", "").strip()

    # Domain-based summaries
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    summary_map = {
        "/": "Entry point for Groq — links to Platform, Solutions, Learn, Pricing, Developers, and Console.",
        "/docs/quickstart": "Step-by-step guide to set up the Groq API: create API key, configure environment, and make first chat completion request.",
        "/docs/models": "Complete list of available models on GroqCloud with pricing, speed, rate limits, and context window details.",
        "/docs/api-reference": "Full API reference for Groq endpoints: Chat completions, Responses, Audio transcription/translation/speech, Models, Batches, Files, Fine-tuning.",
        "/docs/vision": "Documentation for image and vision capabilities using multimodal models for visual question answering, OCR, and image recognition.",
        "/docs/rate-limits": "Rate limit details per model and plan tier, including tokens per minute and requests per minute quotas.",
        "/docs/overview": "Overview of GroqCloud platform, introduction to Groq API and getting started documentation.",
        "/docs/text-chat": "Documentation for text generation and chat completion API usage.",
        "/docs/speech-to-text": "Whisper model documentation for audio transcription and translation.",
        "/docs/text-to-speech": "Text-to-speech documentation and available TTS models.",
        "/docs/reasoning": "Documentation for reasoning capabilities with supported models.",
        "/docs/prompt-caching": "Guide to prompt caching feature for reducing latency and costs on repeated prompts.",
        "/docs/structured-outputs": "Documentation for generating structured JSON outputs from LLM responses.",
        "/docs/tool-use/overview": "Overview of tool use capabilities including built-in tools and local tool calling.",
        "/docs/compound": "Overview of Compound (Agentic AI) system with built-in tools, systems, and use cases.",
        "/docs/prompting": "Prompting guide with basics, patterns, and model migration strategies.",
        "/docs/batch": "Batch processing documentation for high-throughput inference jobs.",
        "/docs/lora": "LoRA (Low-Rank Adaptation) inference documentation for custom model fine-tuning.",
        "/docs/errors": "Error codes reference for Groq API responses.",
        "/playground": "Interactive playground to test Groq API models directly in the browser.",
        "/keys": "API key management: create, view, and manage API keys for GroqCloud access.",
        "/dashboard": "Dashboard overview with account metrics, usage data, logs, and billing information.",
        "/settings": "Account settings and configuration options.",
    }

    # Try exact path match
    if path in summary_map:
        return summary_map[path]

    # Try prefix match
    for prefix, summary in summary_map.items():
        if path.startswith(prefix) and prefix != "/":
            return f"{title_clean}: {summary}"

    # Groq.com marketing pages
    if "groq.com" in parsed.netloc and "console" not in parsed.netloc:
        if "lpu" in path:
            return "Technical deep-dive into Groq's Language Processing Unit (LPU) architecture: design principles, deterministic compute, on-chip memory, and performance advantages over GPUs."
        if "privacy" in path:
            return "Groq privacy policy covering data collection, usage, cookies, and user rights across all Groq services."
        if "pricing" in path:
            return "Groq pricing page with per-model token costs and plan comparisons."

    # Generic fallback
    return f"Page about {title_clean} — contains documentation and controls for {title_clean.lower()} functionality."


# ═══════════════════════════════════════════════════════════════════════
# Tree Builder — Construct the hierarchical site map
# ═══════════════════════════════════════════════════════════════════════

def build_tree_from_pillars(
    pillars: List[PillarSection],
    domain: str,
    use_llm: bool = False,
) -> SiteNode:
    """
    Build a hierarchical SiteNode tree from parsed PILLAR sections.

    Strategy:
    1. Create root node for the domain
    2. Group pages by domain (groq.com vs console.groq.com)
    3. Build hierarchy from URL path structure
    4. Extract navigation structure from sidebar
    5. Enrich with controls and summaries
    """
    root = SiteNode(
        node_id="root",
        title="Groq Platform",
        logical_path="root",
        url=f"https://{domain}/",
        path_regex="^/$",
        summary_of_contents="Entry point for Groq platform — links to Console, Docs, Playground, API Keys, and Dashboard.",
        expected_controls=["Playground", "API Keys", "Dashboard", "Docs", "Settings", "Search"],
    )

    # ── Group pillars by domain ──
    marketing_pillars = []  # groq.com (non-console)
    console_pillars = []     # console.groq.com

    for p in pillars:
        parsed = urlparse(p.url)
        if "console.groq.com" in parsed.netloc:
            console_pillars.append(p)
        else:
            marketing_pillars.append(p)

    # ── Build Console subtree ──
    console_node = SiteNode(
        node_id="console_home",
        title="GroqCloud Console Home",
        logical_path="root.console",
        url="https://console.groq.com/home",
        path_regex="^/(home)?$",
        summary_of_contents="GroqCloud console landing page with navigation to Playground, API Keys, Dashboard, and Docs.",
        expected_controls=["Playground", "API Keys", "Dashboard", "Docs", "Settings", "Login"],
    )

    # ── Build Playground node ──
    playground_node = SiteNode(
        node_id="playground",
        title="Playground",
        logical_path="root.console.playground",
        url="https://console.groq.com/playground",
        path_regex="^/playground",
        summary_of_contents="Interactive playground to test Groq API models directly in the browser with real-time inference.",
        expected_controls=["Model Selector", "System Prompt", "User Message Input", "Send Button", "Temperature Slider"],
        terminal_capabilities=["run_model_inference", "test_prompts", "view_response"],
    )

    # ── Build API Keys node ──
    api_keys_node = SiteNode(
        node_id="api_keys",
        title="API Keys",
        logical_path="root.console.api_keys",
        url="https://console.groq.com/keys",
        path_regex="^/keys",
        summary_of_contents="API key management page: create, view, delete, and manage API keys for GroqCloud access.",
        expected_controls=["Create API Key Button", "Key List", "Copy Key", "Delete Key"],
        terminal_capabilities=["manage_api_keys", "create_key", "view_keys", "delete_key"],
    )

    # ── Build Dashboard subtree ──
    dashboard_node = SiteNode(
        node_id="dashboard_main",
        title="Dashboard Overview",
        logical_path="root.console.dashboard",
        url="https://console.groq.com/dashboard",
        path_regex="^/dashboard$",
        summary_of_contents="High-level account metrics and navigation to specific usage, logs, and batch tabs.",
        expected_controls=["Metrics", "Usage", "Logs", "Batch"],
    )

    usage_node = SiteNode(
        node_id="usage_section",
        title="Usage and Spend",
        logical_path="root.console.dashboard.usage",
        url="https://console.groq.com/dashboard/usage",
        path_regex="^/dashboard/usage",
        summary_of_contents="Detailed model-by-model token consumption and cost data. View usage by date range, filter by model.",
        expected_controls=["Date Picker", "Model Filter", "Activity Tab", "Cost Tab"],
        required_controls=["Date Picker", "Model Filter"],
        terminal_capabilities=["read_token_usage", "filter_by_model", "export_csv", "view_cost_breakdown"],
    )

    logs_node = SiteNode(
        node_id="logs_section",
        title="Logs",
        logical_path="root.console.dashboard.logs",
        url="https://console.groq.com/dashboard/logs",
        path_regex="^/dashboard/logs",
        summary_of_contents="Request logs viewer showing API call history, status codes, and latency metrics.",
        expected_controls=["Log List", "Filter", "Search", "Date Range"],
        terminal_capabilities=["read_request_logs", "filter_logs", "view_error_details"],
    )

    batch_node = SiteNode(
        node_id="batch_section",
        title="Batch Jobs",
        logical_path="root.console.dashboard.batch",
        url="https://console.groq.com/dashboard/batch",
        path_regex="^/dashboard/batch",
        summary_of_contents="Batch job management: create, monitor, and manage batch inference jobs.",
        expected_controls=["Job List", "Create Batch", "Status Filter"],
        terminal_capabilities=["view_batch_jobs", "create_batch", "monitor_status"],
    )

    dashboard_node.children = [usage_node, logs_node, batch_node]

    # ── Build Settings node ──
    settings_node = SiteNode(
        node_id="settings",
        title="Settings",
        logical_path="root.console.settings",
        url="https://console.groq.com/settings",
        path_regex="^/settings",
        summary_of_contents="Account settings, profile, organization, and billing configuration.",
        expected_controls=["Profile", "Organization", "Billing", "Notifications"],
        terminal_capabilities=["update_settings", "manage_billing", "view_profile"],
    )

    # ── Build Documentation subtree ──
    docs_node = SiteNode(
        node_id="docs_root",
        title="Documentation",
        logical_path="root.console.docs",
        url="https://console.groq.com/docs/overview",
        path_regex="^/docs(/overview)?$",
        summary_of_contents="GroqCloud documentation hub with sidebar navigation to all documentation categories.",
        expected_controls=["Sidebar Navigation", "Search", "Docs Link", "API Reference Link"],
    )

    # ── Documentation categories from the sidebar structure ──
    docs_categories = _build_docs_categories(console_pillars, use_llm)
    docs_node.children = docs_categories

    console_node.children = [playground_node, api_keys_node, dashboard_node, docs_node, settings_node]

    # ── Build Marketing subtree (groq.com) ──
    marketing_node = SiteNode(
        node_id="groq_website",
        title="Groq Website",
        logical_path="root.website",
        url="https://groq.com/",
        path_regex="^/$",
        summary_of_contents="Groq marketing website with information about platform, solutions, pricing, LPU architecture, and developer resources.",
        expected_controls=["Platform Menu", "Solutions Menu", "Learn Menu", "Pricing Link", "About Menu", "Developers Menu", "Start Building Button"],
    )

    for p in marketing_pillars:
        parsed = urlparse(p.url)
        path = parsed.path.rstrip("/") or "/"
        slug = _slugify(p.title.split(" - ")[0].split("|")[0])
        if not slug:
            slug = f"pillar_{p.pillar_num}"

        summary = ""
        if use_llm:
            summary = _llm_enrich_summary(p.title, p.url, p.content[:2000])
        if not summary:
            summary = _deterministic_summary(p.title, p.url, p.content)

        expected, required, terminal = _extract_page_controls_from_content(p.content, p.url)

        node = SiteNode(
            node_id=slug,
            title=p.title.split(" - ")[0].strip(),
            logical_path=f"root.website.{slug}",
            url=p.url,
            path_regex=_url_to_path_regex(p.url),
            summary_of_contents=summary,
            expected_controls=expected,
            required_controls=required,
            terminal_capabilities=terminal,
        )
        marketing_node.children.append(node)

    root.children = [console_node, marketing_node]

    return root


def _build_docs_categories(
    console_pillars: List[PillarSection],
    use_llm: bool,
) -> List[SiteNode]:
    """
    Build documentation category nodes from the sidebar navigation
    structure found in console.groq.com pages.
    """
    # Define the full docs sidebar structure
    # This mirrors the actual sidebar from groq.md
    categories = [
        {
            "id": "getting_started",
            "title": "Getting Started",
            "children": [
                ("docs_overview", "Overview", "/docs/overview"),
                ("docs_quickstart", "Quickstart", "/docs/quickstart"),
                ("docs_models", "Supported Models", "/docs/models"),
                ("docs_openai_compat", "OpenAI Compatibility", "/docs/openai"),
                ("docs_responses_api", "Responses API", "/docs/responses-api"),
                ("docs_rate_limits", "Rate Limits", "/docs/rate-limits"),
                ("docs_templates", "Templates", "/docs/examples"),
                ("docs_api_reference", "API Reference", "/docs/api-reference"),
            ],
        },
        {
            "id": "core_features",
            "title": "Core Features",
            "children": [
                ("docs_text_gen", "Text Generation", "/docs/text-chat"),
                ("docs_stt", "Speech to Text", "/docs/speech-to-text"),
                ("docs_tts", "Text to Speech", "/docs/text-to-speech"),
                ("docs_orpheus", "Orpheus", "/docs/text-to-speech/orpheus"),
                ("docs_vision", "OCR and Image Recognition", "/docs/vision"),
                ("docs_reasoning", "Reasoning", "/docs/reasoning"),
                ("docs_moderation", "Content Moderation", "/docs/content-moderation"),
                ("docs_structured", "Structured Outputs", "/docs/structured-outputs"),
                ("docs_prompt_caching", "Prompt Caching", "/docs/prompt-caching"),
            ],
        },
        {
            "id": "tools_integrations",
            "title": "Tools & Integrations",
            "children": [
                ("docs_tool_use", "Tool Use Overview", "/docs/tool-use/overview"),
                ("docs_builtin_tools", "Groq Built-In Tools", "/docs/tool-use/built-in-tools"),
                ("docs_web_search", "Web Search", "/docs/tool-use/built-in-tools/web-search"),
                ("docs_visit_website", "Visit Website", "/docs/tool-use/built-in-tools/visit-website"),
                ("docs_browser_auto", "Browser Automation", "/docs/tool-use/built-in-tools/browser-automation"),
                ("docs_code_exec", "Code Execution", "/docs/tool-use/built-in-tools/code-execution"),
                ("docs_wolfram", "Wolfram Alpha", "/docs/tool-use/built-in-tools/wolfram-alpha"),
                ("docs_browser_search", "Browser Search (GPT OSS)", "/docs/tool-use/built-in-tools/browser-search"),
                ("docs_remote_mcp", "Remote Tools and MCP", "/docs/tool-use/remote-mcp"),
                ("docs_connectors", "Connectors", "/docs/tool-use/remote-mcp/connectors"),
                ("docs_local_tool", "Local Tool Calling", "/docs/tool-use/local-tool-calling"),
                ("docs_integrations", "Integrations Catalog", "/docs/integrations"),
                ("docs_coding", "Coding with Groq", "/docs/coding-with-groq"),
            ],
        },
        {
            "id": "compound_agentic",
            "title": "Compound (Agentic AI)",
            "children": [
                ("docs_compound_overview", "Overview", "/docs/compound"),
                ("docs_compound_tools", "Built-In Tools", "/docs/compound/built-in-tools"),
                ("docs_compound_systems", "Systems", "/docs/compound/systems"),
                ("docs_compound_usecases", "Use Cases", "/docs/compound/use-cases"),
            ],
        },
        {
            "id": "guides",
            "title": "Guides",
            "children": [
                ("docs_prompting", "Prompting Guide", "/docs/prompting"),
                ("docs_patterns", "Patterns", "/docs/prompting/patterns"),
                ("docs_migration", "Model Migration", "/docs/prompting/model-migration"),
                ("docs_prefilling", "Assistant Message Prefilling", "/docs/prefilling"),
            ],
        },
        {
            "id": "service_tiers",
            "title": "Service Tiers",
            "children": [
                ("docs_tiers_overview", "Service Tiers", "/docs/service-tiers"),
                ("docs_performance", "Performance Tier", "/docs/performance-tier"),
                ("docs_flex", "Flex Processing", "/docs/flex-processing"),
                ("docs_batch", "Batch Processing", "/docs/batch"),
            ],
        },
        {
            "id": "advanced",
            "title": "Advanced",
            "children": [
                ("docs_lora", "LoRA Inference", "/docs/lora"),
            ],
        },
        {
            "id": "production",
            "title": "Production Readiness",
            "children": [
                ("docs_prod_checklist", "Production Checklist", "/docs/production-readiness/production-ready-checklist"),
                ("docs_optimize_latency", "Optimizing Latency", "/docs/production-readiness/optimizing-latency"),
                ("docs_security", "Security Onboarding", "/docs/production-readiness/security-onboarding"),
                ("docs_prometheus", "Prometheus Metrics", "/docs/prometheus-metrics"),
            ],
        },
        {
            "id": "account",
            "title": "Account and Console",
            "children": [
                ("docs_spend_limits", "Spend Limits", "/docs/spend-limits"),
                ("docs_projects", "Projects", "/docs/projects"),
                ("docs_model_perms", "Model Permissions", "/docs/model-permissions"),
                ("docs_billing", "Billing FAQs", "/docs/billing-faqs"),
                ("docs_your_data", "Your Data", "/docs/your-data"),
            ],
        },
        {
            "id": "developer_resources",
            "title": "Developer Resources",
            "children": [
                ("docs_libraries", "SDK Libraries", "/docs/libraries"),
                ("docs_badge", "Groq Badge", "/docs/badge"),
                ("docs_errors", "Error Codes", "/docs/errors"),
                ("docs_changelog", "Changelog", "/docs/changelog"),
            ],
        },
        {
            "id": "legal",
            "title": "Legal",
            "children": [
                ("docs_legal", "Policies & Notices", "/docs/legal"),
            ],
        },
    ]

    # Build pillar content index for LLM enrichment
    pillar_by_path: Dict[str, PillarSection] = {}
    for p in console_pillars:
        parsed = urlparse(p.url)
        pillar_by_path[parsed.path.rstrip("/")] = p

    category_nodes: List[SiteNode] = []
    for cat in categories:
        cat_node = SiteNode(
            node_id=cat["id"],
            title=cat["title"],
            logical_path=f"root.console.docs.{cat['id']}",
            summary_of_contents=f"Documentation category: {cat['title']}. Contains {len(cat['children'])} sub-pages.",
            expected_controls=["Sidebar Navigation", "Search"],
        )

        for child_id, child_title, child_path in cat["children"]:
            full_url = f"https://console.groq.com{child_path}"

            # Get summary
            summary = ""
            pillar = pillar_by_path.get(child_path.rstrip("/"))

            if use_llm and pillar:
                summary = _llm_enrich_summary(child_title, full_url, pillar.content[:2000])
                time.sleep(0.1)  # rate limit courtesy

            if not summary:
                summary = _deterministic_summary(child_title, full_url, pillar.content if pillar else "")

            expected, required, terminal = _extract_page_controls_from_content(
                pillar.content if pillar else "", full_url
            )
            if not terminal:
                terminal = ["read_documentation"]

            child_node = SiteNode(
                node_id=child_id,
                title=child_title,
                logical_path=f"root.console.docs.{cat['id']}.{child_id}",
                url=full_url,
                path_regex=_url_to_path_regex(full_url),
                summary_of_contents=summary,
                expected_controls=expected or ["Sidebar Navigation", "Content Area"],
                required_controls=required,
                terminal_capabilities=terminal,
            )
            cat_node.children.append(child_node)

        category_nodes.append(cat_node)

    return category_nodes


# ═══════════════════════════════════════════════════════════════════════
# Statistics & Validation
# ═══════════════════════════════════════════════════════════════════════

def _count_nodes(node: SiteNode) -> int:
    count = 1
    for child in node.children:
        count += _count_nodes(child)
    return count


def _count_leaf_nodes(node: SiteNode) -> int:
    if not node.children:
        return 1
    count = 0
    for child in node.children:
        count += _count_leaf_nodes(child)
    return count


def _max_depth(node: SiteNode, depth: int = 0) -> int:
    if not node.children:
        return depth
    return max(_max_depth(child, depth + 1) for child in node.children)


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Build a hierarchical PageIndex (site_map.json) from groq.md"
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to the structured markdown file (e.g., groq.md)",
    )
    parser.add_argument(
        "--output", "-o",
        default="site_map.json",
        help="Output JSON file path (default: site_map.json)",
    )
    parser.add_argument(
        "--domain", "-d",
        default="console.groq.com",
        help="Primary domain for the site map (default: console.groq.com)",
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Use openai/gpt-oss-20b to enrich node summaries",
    )
    args = parser.parse_args()

    # ── Read input ──
    logger.info(f"Reading input: {args.input}")
    with open(args.input, "r", encoding="utf-8") as f:
        md_text = f.read()

    logger.info(f"Input size: {len(md_text)} chars, {md_text.count(chr(10))} lines")

    # ── Parse PILLARs ──
    pillars = parse_pillars(md_text)
    logger.info(f"Parsed {len(pillars)} PILLAR sections:")
    for p in pillars:
        logger.info(f"  PILLAR {p.pillar_num}: {p.title} ({p.url})")

    if not pillars:
        logger.error("No PILLAR sections found in input file!")
        sys.exit(1)

    # ── Build tree ──
    logger.info(f"Building site tree for domain: {args.domain} (use_llm={args.use_llm})")
    root = build_tree_from_pillars(pillars, args.domain, use_llm=args.use_llm)

    # ── Assemble output ──
    site_map = {
        "site_metadata": {
            "domain": args.domain,
            "version": "1.0.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_file": os.path.basename(args.input),
            "pillar_count": len(pillars),
        },
        "root": root.to_dict(),
    }

    # ── Write output ──
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(site_map, f, indent=2, ensure_ascii=False)

    total_nodes = _count_nodes(root)
    leaf_nodes = _count_leaf_nodes(root)
    depth = _max_depth(root)
    file_size = os.path.getsize(args.output)

    logger.info(f"✅ Site map written to: {args.output}")
    logger.info(f"   Total nodes:  {total_nodes}")
    logger.info(f"   Leaf nodes:   {leaf_nodes}")
    logger.info(f"   Max depth:    {depth}")
    logger.info(f"   File size:    {file_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()

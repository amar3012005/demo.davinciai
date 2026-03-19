#!/usr/bin/env python3
"""
build_site_index.py  —  PageIndex Site Indexer

Build a hierarchical site_map.json from a structured markdown source and
upgrade each node from a descriptive sitemap entry into an operational
page contract that downstream navigation and last-mile stages can treat as
ground truth.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("build_site_index")


def _dedupe(items: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _merge_dicts(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalize_slug(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s_-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:60].strip("_")


def _title_variants(title: str) -> List[str]:
    base = title.replace(" - GroqDocs", "").replace("| Groq", "").strip()
    variants = [base]
    if base.lower().startswith("groqcloud "):
        variants.append(base.replace("GroqCloud ", "", 1))
    return _dedupe([v for v in variants if v])


def _url_to_path_regex(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    escaped = re.escape(path)
    if path == "/":
        return r"^/$"
    return f"^{escaped}/?$"


def _make_control_group(
    labels: List[str],
    zones: List[str],
    element_types: List[str],
    *,
    required: bool = False,
    unlocks: Optional[List[str]] = None,
    opens: Optional[List[str]] = None,
) -> Dict[str, Any]:
    group: Dict[str, Any] = {
        "labels": _dedupe(labels),
        "zones": _dedupe(zones),
        "element_types": _dedupe(element_types),
        "required": required,
    }
    if unlocks:
        group["unlocks"] = _dedupe(unlocks)
    if opens:
        group["opens"] = _dedupe(opens)
    return group


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
    page_type: str = ""
    node_aliases: List[str] = field(default_factory=list)
    goal_triggers: Dict[str, List[str]] = field(default_factory=dict)
    control_groups: Dict[str, Any] = field(default_factory=dict)
    task_modes: Dict[str, Any] = field(default_factory=dict)
    transition_rules: Dict[str, Any] = field(default_factory=dict)
    completion_contracts: Dict[str, Any] = field(default_factory=dict)
    evidence_zones: List[str] = field(default_factory=list)
    verification_signals: List[str] = field(default_factory=list)
    children: List["SiteNode"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "logical_path": self.logical_path,
            "url": self.url,
            "path_regex": self.path_regex,
            "summary_of_contents": self.summary_of_contents,
            "expected_controls": self.expected_controls,
            "required_controls": self.required_controls,
            "terminal_capabilities": self.terminal_capabilities,
            "page_type": self.page_type,
            "node_aliases": self.node_aliases,
            "goal_triggers": self.goal_triggers,
            "control_groups": self.control_groups,
            "task_modes": self.task_modes,
            "transition_rules": self.transition_rules,
            "completion_contracts": self.completion_contracts,
            "evidence_zones": self.evidence_zones,
            "verification_signals": self.verification_signals,
            "children": [c.to_dict() for c in self.children],
        }


# ═══════════════════════════════════════════════════════════════════════
# Markdown Parsing
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class PillarSection:
    pillar_num: int
    url: str
    title: str
    content: str


def parse_pillars(md_text: str) -> List[PillarSection]:
    pattern = re.compile(r"^## PILLAR (\d+):\s*(https?://\S+)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(md_text))
    sections: List[PillarSection] = []

    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        block = md_text[start:end].strip()
        title_match = re.search(r"^Title:\s*(.+)$", block, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else f"Pillar {match.group(1)}"
        sections.append(
            PillarSection(
                pillar_num=int(match.group(1)),
                url=match.group(2).strip(),
                title=title,
                content=block,
            )
        )

    return sections


# ═══════════════════════════════════════════════════════════════════════
# Deterministic Contract Inference
# ═══════════════════════════════════════════════════════════════════════


def _deterministic_summary(title: str, url: str, content: str) -> str:
    title_clean = title.replace(" - GroqDocs", "").strip()
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    summary_map = {
        "/": "Entry point for Groq and GroqCloud navigation, including links to docs, console, pricing, and product surfaces.",
        "/home": "Console landing page that routes users to playground, keys, dashboard, docs, and settings.",
        "/playground": "Interactive playground for model selection, prompt editing, and response inspection.",
        "/keys": "API key management workspace for creating, viewing, copying, and deleting keys.",
        "/dashboard": "Dashboard overview for usage, logs, batch jobs, and account metrics.",
        "/dashboard/usage": "Usage analytics page with date, model, and metric controls for reading token and spend data.",
        "/dashboard/logs": "Logs viewer for request history, status codes, latency, and filtered troubleshooting.",
        "/dashboard/batch": "Batch jobs page for creating, inspecting, and monitoring batch inference work.",
        "/settings": "Settings area for account, organization, billing, and notification configuration.",
        "/docs/overview": "Documentation hub that introduces GroqCloud concepts and routes into the docs tree.",
        "/docs/quickstart": "Quickstart guide for creating an API key, configuring environment variables, and sending the first request.",
        "/docs/models": "Model catalog with supported models, capabilities, pricing, and context window details.",
        "/docs/api-reference": "API reference with endpoint definitions, request schemas, and parameter details.",
        "/docs/vision": "Vision documentation covering OCR and image understanding capabilities.",
        "/docs/rate-limits": "Rate limit documentation for request and token quotas by model or tier.",
        "/docs/prompt-caching": "Prompt caching guide describing cache behavior, eligibility, and usage patterns.",
        "/docs/batch": "Batch processing documentation covering job creation, lifecycle, and constraints.",
    }

    if path in summary_map:
        return summary_map[path]
    for prefix, summary in summary_map.items():
        if prefix != "/" and path.startswith(prefix):
            return f"{title_clean}: {summary}"

    if "console.groq.com" in parsed.netloc and path.startswith("/docs"):
        return f"Documentation page for {title_clean}, including reference material and navigation to related docs."
    if "groq.com" in parsed.netloc and "console" not in parsed.netloc:
        return f"Marketing or informational page for {title_clean} on groq.com."
    return f"Page for {title_clean} with controls and content related to {title_clean.lower()}."


def _infer_page_type(url: str, title: str, content: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    title_l = title.lower()
    content_l = content.lower()

    if path == "/":
        return "dashboard_overview" if "console.groq.com" in parsed.netloc else "docs_index"
    if "playground" in path or "playground" in title_l:
        return "playground"
    if path.startswith("/keys"):
        return "management_list"
    if path == "/dashboard":
        return "dashboard_overview"
    if path.startswith("/dashboard/usage"):
        return "dashboard_data"
    if path.startswith("/dashboard/logs"):
        return "logs_view"
    if path.startswith("/dashboard/batch"):
        return "management_list"
    if path.startswith("/settings"):
        return "settings_page"
    if path == "/docs" or path == "/docs/overview":
        return "docs_index"
    if path.startswith("/docs/api-reference"):
        return "docs_leaf"
    if path.startswith("/docs"):
        return "docs_leaf"
    if any(token in content_l for token in ["create", "new", "wizard", "step 1"]):
        return "creation_flow"
    return "docs_leaf" if "docs" in path else "dashboard_overview"


def _infer_terminal_capabilities(url: str, page_type: str) -> List[str]:
    path = urlparse(url).path.rstrip("/") or "/"
    capabilities: List[str] = []

    if page_type in {"docs_index", "docs_leaf"}:
        capabilities.append("read_documentation")
    if page_type == "playground":
        capabilities.extend(["run_model_inference", "test_prompts", "view_response"])
    if path.startswith("/keys"):
        capabilities.extend(["manage_api_keys", "create_key", "view_keys", "delete_key", "copy_key"])
    if path == "/dashboard":
        capabilities.extend(["view_metrics", "navigate_dashboard_tabs"])
    if path.startswith("/dashboard/usage"):
        capabilities.extend(["read_token_usage", "filter_by_model", "set_date_range", "view_cost_breakdown"])
    if path.startswith("/dashboard/logs"):
        capabilities.extend(["read_request_logs", "filter_logs", "search_logs", "view_error_details"])
    if path.startswith("/dashboard/batch"):
        capabilities.extend(["view_batch_jobs", "create_batch", "monitor_status"])
    if path.startswith("/settings"):
        capabilities.extend(["update_settings", "manage_billing", "view_profile"])
    if path.startswith("/docs/models"):
        capabilities.extend(["read_model_list", "compare_models", "view_pricing"])
    if path.startswith("/docs/api-reference"):
        capabilities.extend(["read_api_spec", "view_endpoints"])
    if path.startswith("/docs/rate-limits"):
        capabilities.extend(["read_rate_limits", "compare_tiers"])
    if path.startswith("/docs/vision"):
        capabilities.extend(["read_vision_docs", "view_code_examples"])
    if path.startswith("/docs/quickstart"):
        capabilities.extend(["read_quickstart", "view_setup_steps"])
    if path.startswith("/docs/prompt-caching"):
        capabilities.append("read_prompt_caching_docs")

    return _dedupe(capabilities)


def _infer_control_groups(url: str, title: str, content: str, page_type: str) -> Dict[str, Any]:
    path = urlparse(url).path.rstrip("/") or "/"
    groups: Dict[str, Any] = {}

    if page_type in {"docs_index", "docs_leaf"}:
        groups["sidebar_navigation"] = _make_control_group(
            ["Sidebar Navigation", "Docs Sidebar", "Navigation"],
            ["left_sidebar"],
            ["nav", "link_list"],
            required=page_type == "docs_index",
            unlocks=["docs_leaf_navigation"],
        )
        groups["docs_search"] = _make_control_group(
            ["Search", "Search docs", "Ask AI"],
            ["header", "left_sidebar"],
            ["input", "button", "combobox"],
        )
        groups["content_area"] = _make_control_group(
            ["Content Area", title],
            ["main"],
            ["article", "section"],
            required=True,
        )

    if page_type == "playground":
        groups["model_selector"] = _make_control_group(
            ["Model Selector", "Model", "Choose model"],
            ["header", "main"],
            ["combobox", "button", "dropdown"],
            required=True,
            unlocks=["response_generation"],
        )
        groups["system_prompt_input"] = _make_control_group(
            ["System Prompt", "System message"],
            ["main"],
            ["textarea", "input"],
        )
        groups["message_input"] = _make_control_group(
            ["User Message Input", "Prompt", "Message"],
            ["main"],
            ["textarea", "input", "editor"],
            required=True,
        )
        groups["send_button"] = _make_control_group(
            ["Send", "Run", "Submit"],
            ["main", "footer"],
            ["button"],
            required=True,
        )
        groups["response_panel"] = _make_control_group(
            ["Response", "Output", "Model Response"],
            ["main", "right_panel"],
            ["panel", "section", "code_block"],
            required=True,
        )

    if path.startswith("/keys"):
        groups["create_api_key_button"] = _make_control_group(
            ["Create API Key", "Create key", "New API Key"],
            ["main", "header", "top_right"],
            ["button", "link"],
            required=True,
            opens=["api_key_creation_modal"],
        )
        groups["key_list"] = _make_control_group(
            ["Key List", "API Keys", "Keys"],
            ["main"],
            ["table", "list"],
            required=True,
        )
        groups["copy_key_button"] = _make_control_group(
            ["Copy Key", "Copy", "Reveal secret"],
            ["main"],
            ["button", "icon_button"],
        )
        groups["delete_key_button"] = _make_control_group(
            ["Delete", "Trash", "Remove"],
            ["main"],
            ["button", "icon_button"],
        )
        groups["api_key_name_input"] = _make_control_group(
            ["API Key Name", "Key name", "Name"],
            ["modal", "main"],
            ["input", "textarea"],
        )
        groups["confirm_create_button"] = _make_control_group(
            ["Create", "Confirm", "Generate key"],
            ["modal", "footer"],
            ["button"],
        )
        groups["confirm_delete_button"] = _make_control_group(
            ["Delete", "Confirm delete", "Remove"],
            ["modal", "footer"],
            ["button"],
        )

    if path == "/dashboard":
        groups["usage_tab"] = _make_control_group(["Usage"], ["main"], ["tab", "button"], required=True)
        groups["logs_tab"] = _make_control_group(["Logs"], ["main"], ["tab", "button"], required=True)
        groups["batch_tab"] = _make_control_group(["Batch"], ["main"], ["tab", "button"], required=True)
        groups["metrics_cards"] = _make_control_group(
            ["Metrics", "Usage cards", "Account metrics"],
            ["main"],
            ["card", "panel"],
        )

    if path.startswith("/dashboard/usage"):
        groups["date_picker"] = _make_control_group(
            ["Date Picker", "Last 7 days", "Last 30 days", "Custom range"],
            ["header", "main"],
            ["button", "combobox", "dropdown"],
            required=True,
            unlocks=["usage_table_refresh"],
        )
        groups["model_filter"] = _make_control_group(
            ["Model Filter", "Model", "All models"],
            ["main", "header"],
            ["input", "combobox", "button"],
            required=True,
            unlocks=["usage_table_refresh"],
        )
        groups["metric_tab"] = _make_control_group(
            ["Activity", "Cost", "Usage"],
            ["main"],
            ["tab", "button"],
            required=True,
        )
        groups["usage_table"] = _make_control_group(
            ["Usage Table", "Usage data", "Spend table"],
            ["main"],
            ["table", "chart", "grid"],
            required=True,
        )
        groups["export_csv_button"] = _make_control_group(
            ["Export CSV", "Download CSV", "Export"],
            ["main", "header"],
            ["button", "link"],
        )

    if path.startswith("/dashboard/logs"):
        groups["log_list"] = _make_control_group(
            ["Log List", "Logs", "Requests"],
            ["main"],
            ["table", "list"],
            required=True,
        )
        groups["date_range"] = _make_control_group(
            ["Date Range", "Date Picker", "Last 24 hours"],
            ["header", "main"],
            ["button", "combobox", "dropdown"],
        )
        groups["log_filter"] = _make_control_group(
            ["Filter", "Status Filter", "Model Filter"],
            ["main", "header"],
            ["combobox", "button", "input"],
        )
        groups["log_search"] = _make_control_group(
            ["Search", "Search logs"],
            ["header", "main"],
            ["input", "searchbox"],
        )

    if path.startswith("/dashboard/batch"):
        groups["job_list"] = _make_control_group(
            ["Job List", "Batch Jobs", "Jobs"],
            ["main"],
            ["table", "list"],
            required=True,
        )
        groups["create_batch_button"] = _make_control_group(
            ["Create Batch", "New Batch", "Upload Batch"],
            ["main", "header", "top_right"],
            ["button", "link"],
            required=True,
        )
        groups["status_filter"] = _make_control_group(
            ["Status Filter", "Status", "Job status"],
            ["main", "header"],
            ["combobox", "button"],
        )

    if page_type == "settings_page":
        groups["settings_tabs"] = _make_control_group(
            ["Profile", "Organization", "Billing", "Notifications"],
            ["left_sidebar", "main"],
            ["tab", "link", "button"],
            required=True,
        )
        groups["save_button"] = _make_control_group(
            ["Save", "Update", "Apply"],
            ["main", "footer"],
            ["button"],
        )

    if not groups and ("search" in content.lower() or "filter" in content.lower()):
        groups["content_area"] = _make_control_group([title], ["main"], ["section"], required=True)

    return groups


def _infer_task_modes(
    url: str,
    title: str,
    content: str,
    page_type: str,
    control_groups: Dict[str, Any],
) -> Dict[str, Any]:
    path = urlparse(url).path.rstrip("/") or "/"
    task_modes: Dict[str, Any] = {}

    if page_type == "playground":
        task_modes["run_model_inference"] = {
            "task_type": "create_action",
            "primary_cta_group": "send_button",
            "post_click_expected": ["response_panel"],
            "success_evidence": ["response_rendered", "latency_visible"],
        }

    if path.startswith("/keys"):
        task_modes["create_api_key"] = {
            "task_type": "create_action",
            "primary_cta_group": "create_api_key_button",
            "post_click_expected": ["api_key_name_input", "confirm_create_button", "modal"],
            "success_evidence": ["new_key_visible", "secret_key_shown", "success_toast"],
        }
        task_modes["delete_api_key"] = {
            "task_type": "destructive_action",
            "primary_cta_group": "delete_key_button",
            "post_click_expected": ["confirm_delete_button", "modal"],
            "success_evidence": ["row_removed", "success_toast"],
        }
        task_modes["copy_api_key"] = {
            "task_type": "confirm_action",
            "primary_cta_group": "copy_key_button",
            "success_evidence": ["clipboard_success", "success_toast"],
        }

    if path == "/dashboard":
        task_modes["open_usage_section"] = {
            "task_type": "navigation",
            "primary_cta_group": "usage_tab",
            "success_evidence": ["usage_section_loaded"],
        }
        task_modes["open_logs_section"] = {
            "task_type": "navigation",
            "primary_cta_group": "logs_tab",
            "success_evidence": ["logs_section_loaded"],
        }
        task_modes["open_batch_section"] = {
            "task_type": "navigation",
            "primary_cta_group": "batch_tab",
            "success_evidence": ["batch_section_loaded"],
        }

    if path.startswith("/dashboard/usage"):
        task_modes["read_token_usage"] = {
            "task_type": "read_extract",
            "transition_recipe": [
                "set_date_picker",
                "set_model_filter",
                "set_metric_tab",
                "extract_usage_value",
            ],
            "completion_contract": {
                "requires": ["entity_anchor", "metric_anchor", "numeric_value"],
                "disallow": ["url_only_evidence", "vision_only_evidence"],
            },
        }
        task_modes["read_cost_breakdown"] = {
            "task_type": "read_extract",
            "transition_recipe": [
                "set_date_picker",
                "set_model_filter",
                "set_metric_tab",
                "extract_cost_value",
            ],
            "completion_contract": {
                "requires": ["metric_anchor", "numeric_value"],
                "disallow": ["url_only_evidence", "vision_only_evidence"],
            },
        }

    if path.startswith("/dashboard/logs"):
        task_modes["read_request_logs"] = {
            "task_type": "read_extract",
            "transition_recipe": [
                "set_date_range",
                "apply_log_filter",
                "search_logs",
                "extract_log_row",
            ],
            "completion_contract": {
                "requires": ["log_row_anchor", "status_anchor"],
                "disallow": ["url_only_evidence", "vision_only_evidence"],
            },
        }

    if path.startswith("/dashboard/batch"):
        task_modes["create_batch"] = {
            "task_type": "create_action",
            "primary_cta_group": "create_batch_button",
            "post_click_expected": ["upload_form", "modal", "file_input"],
            "success_evidence": ["job_created", "success_toast", "new_job_visible"],
        }
        task_modes["monitor_batch_status"] = {
            "task_type": "read_extract",
            "transition_recipe": ["set_status_filter", "open_job_row", "extract_status"],
            "completion_contract": {
                "requires": ["job_anchor", "status_anchor"],
                "disallow": ["url_only_evidence"],
            },
        }

    if page_type == "settings_page":
        task_modes["update_settings"] = {
            "task_type": "form_fill",
            "primary_cta_group": "save_button",
            "success_evidence": ["success_toast", "updated_value_visible"],
        }

    if page_type in {"docs_index", "docs_leaf"}:
        task_modes["read_documentation"] = {
            "task_type": "read_extract",
            "transition_recipe": ["locate_content_area", "extract_answer"],
            "completion_contract": {
                "requires": ["content_anchor", "text_evidence"],
                "disallow": ["url_only_evidence"],
            },
        }
        if "api-reference" in path:
            task_modes["read_api_reference"] = {
                "task_type": "read_extract",
                "transition_recipe": ["locate_content_area", "extract_endpoint_schema"],
                "completion_contract": {
                    "requires": ["endpoint_anchor", "parameter_anchor"],
                    "disallow": ["url_only_evidence"],
                },
            }

    if not task_modes and control_groups:
        task_modes["inspect_page"] = {
            "task_type": "read_extract",
            "transition_recipe": ["locate_content_area", "extract_answer"],
            "completion_contract": {
                "requires": ["content_anchor"],
                "disallow": ["url_only_evidence"],
            },
        }

    return task_modes


def _infer_transition_rules(
    page_type: str,
    task_modes: Dict[str, Any],
    control_groups: Dict[str, Any],
) -> Dict[str, Any]:
    rules: Dict[str, Any] = {}

    for task_name, task_mode in task_modes.items():
        task_type = task_mode.get("task_type")
        if task_name == "create_api_key":
            rules[task_name] = [
                {"step": "click_primary_cta", "control_group": "create_api_key_button", "verify": ["modal_open", "input_visible"]},
                {"step": "fill_name", "control_group": "api_key_name_input", "optional": True},
                {"step": "confirm_create", "control_group": "confirm_create_button", "verify": ["new_key_visible", "secret_key_shown", "success_toast"]},
            ]
            continue
        if task_name == "delete_api_key":
            rules[task_name] = [
                {"step": "select_key_row", "control_group": "key_list", "verify": ["row_selected"]},
                {"step": "click_delete", "control_group": "delete_key_button", "verify": ["modal_open"]},
                {"step": "confirm_delete", "control_group": "confirm_delete_button", "verify": ["row_removed", "success_toast"]},
            ]
            continue
        if task_name == "read_token_usage":
            rules[task_name] = [
                {"step": "set_date_picker", "control_group": "date_picker", "verify": ["date_filter_applied"]},
                {"step": "set_model_filter", "control_group": "model_filter", "optional": True, "verify": ["model_filter_applied"]},
                {"step": "set_metric_tab", "control_group": "metric_tab", "verify": ["metric_view_loaded"]},
                {"step": "extract_usage_value", "control_group": "usage_table", "verify": ["entity_anchor", "metric_anchor", "numeric_value"]},
            ]
            continue
        if task_name == "read_cost_breakdown":
            rules[task_name] = [
                {"step": "set_date_picker", "control_group": "date_picker", "verify": ["date_filter_applied"]},
                {"step": "set_model_filter", "control_group": "model_filter", "optional": True, "verify": ["model_filter_applied"]},
                {"step": "set_metric_tab", "control_group": "metric_tab", "verify": ["metric_view_loaded"]},
                {"step": "extract_cost_value", "control_group": "usage_table", "verify": ["metric_anchor", "numeric_value"]},
            ]
            continue
        if task_name == "read_request_logs":
            rules[task_name] = [
                {"step": "set_date_range", "control_group": "date_range", "optional": True, "verify": ["date_filter_applied"]},
                {"step": "apply_log_filter", "control_group": "log_filter", "optional": True, "verify": ["log_filter_applied"]},
                {"step": "search_logs", "control_group": "log_search", "optional": True, "verify": ["search_applied"]},
                {"step": "extract_log_row", "control_group": "log_list", "verify": ["log_row_anchor", "status_anchor"]},
            ]
            continue
        if task_name == "create_batch":
            rules[task_name] = [
                {"step": "click_primary_cta", "control_group": "create_batch_button", "verify": ["modal_open", "file_input_visible"]},
                {"step": "upload_batch_input", "control_group": "create_batch_button", "optional": True},
                {"step": "confirm_batch_create", "control_group": "create_batch_button", "verify": ["job_created", "success_toast"]},
            ]
            continue

        if task_type in {"read_extract", "navigation"}:
            primary = task_mode.get("primary_cta_group")
            recipe = []
            for step_name in task_mode.get("transition_recipe", []):
                inferred_group = ""
                if step_name.startswith("set_date") and "date_picker" in control_groups:
                    inferred_group = "date_picker"
                elif "model_filter" in step_name and "model_filter" in control_groups:
                    inferred_group = "model_filter"
                elif "metric" in step_name and "metric_tab" in control_groups:
                    inferred_group = "metric_tab"
                elif "content" in step_name and "content_area" in control_groups:
                    inferred_group = "content_area"
                elif "search" in step_name and "log_search" in control_groups:
                    inferred_group = "log_search"
                elif "filter" in step_name and "log_filter" in control_groups:
                    inferred_group = "log_filter"
                recipe.append({"step": step_name, "control_group": inferred_group} if inferred_group else {"step": step_name})
            if primary:
                recipe.insert(0, {"step": "activate_primary", "control_group": primary})
            rules[task_name] = recipe
            continue

        if task_type in {"create_action", "form_fill", "confirm_action"}:
            primary = task_mode.get("primary_cta_group")
            if primary:
                rules[task_name] = [{"step": "activate_primary", "control_group": primary}]

    return rules


def _infer_completion_contracts(page_type: str, task_modes: Dict[str, Any]) -> Dict[str, Any]:
    contracts: Dict[str, Any] = {}
    for task_name, task_mode in task_modes.items():
        embedded = task_mode.get("completion_contract")
        if embedded:
            requires = embedded.get("requires", [])
            contracts[task_name] = {
                "requires_all": requires,
                "disallow": embedded.get("disallow", []),
            }
            continue
        success_evidence = task_mode.get("success_evidence", [])
        if success_evidence:
            contracts[task_name] = {
                "requires_any": success_evidence,
                "disallow": ["cta_visible_only", "url_only_evidence"],
            }
        elif task_mode.get("task_type") == "read_extract":
            contracts[task_name] = {
                "requires_all": ["content_anchor", "text_evidence"],
                "disallow": ["url_only_evidence"],
            }
    return contracts


def _infer_goal_triggers(task_modes: Dict[str, Any], title: str, page_type: str) -> Dict[str, List[str]]:
    triggers: Dict[str, List[str]] = {}
    title_l = title.lower()
    for task_name in task_modes:
        phrases = [task_name.replace("_", " ")]
        if task_name == "create_api_key":
            phrases.extend(["create api key", "new api key", "generate key", "make me a key"])
        elif task_name == "delete_api_key":
            phrases.extend(["delete api key", "remove key", "revoke key"])
        elif task_name == "copy_api_key":
            phrases.extend(["copy api key", "copy key", "show secret key"])
        elif task_name == "read_token_usage":
            phrases.extend(["read token usage", "check usage", "see token spend", "usage for a model"])
        elif task_name == "read_cost_breakdown":
            phrases.extend(["view costs", "cost breakdown", "spend by model"])
        elif task_name == "read_request_logs":
            phrases.extend(["view logs", "check request logs", "find request history"])
        elif task_name == "run_model_inference":
            phrases.extend(["test prompt", "run inference", "use playground"])
        elif task_name == "read_api_reference":
            phrases.extend(["api reference", "endpoint docs", "request schema"])
        elif task_name == "read_documentation":
            phrases.extend([f"read {title_l}", f"docs for {title_l}"])
        elif task_name == "update_settings":
            phrases.extend(["change settings", "update profile", "manage billing"])
        triggers[task_name] = _dedupe(phrases)
    return triggers


def _infer_node_aliases(title: str, url: str, page_type: str) -> List[str]:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    aliases = _title_variants(title)
    if path == "/keys":
        aliases.extend(["keys page", "api key settings", "api keys page"])
    if path == "/dashboard/usage":
        aliases.extend(["usage page", "spend dashboard", "usage section"])
    if path == "/dashboard/logs":
        aliases.extend(["logs page", "request logs"])
    if path == "/dashboard/batch":
        aliases.extend(["batch jobs", "batch section"])
    if page_type == "docs_leaf":
        aliases.extend([f"{title} docs", f"{title} documentation"])
    return _dedupe(aliases)


def _infer_evidence_zones(page_type: str, control_groups: Dict[str, Any]) -> List[str]:
    zones: List[str] = ["main"]
    for group in control_groups.values():
        zones.extend(group.get("zones", []))
    if page_type in {"management_list", "logs_view"}:
        zones.append("table")
    if page_type == "settings_page":
        zones.append("modal")
    return _dedupe(zones)


def _infer_verification_signals(
    control_groups: Dict[str, Any],
    task_modes: Dict[str, Any],
    completion_contracts: Dict[str, Any],
    transition_rules: Dict[str, Any],
) -> List[str]:
    signals: List[str] = []
    for task_mode in task_modes.values():
        signals.extend(task_mode.get("success_evidence", []))
        completion_contract = task_mode.get("completion_contract", {})
        signals.extend(completion_contract.get("requires", []))
    for contract in completion_contracts.values():
        signals.extend(contract.get("requires_all", []))
        signals.extend(contract.get("requires_any", []))
    for steps in transition_rules.values():
        for step in steps:
            signals.extend(step.get("verify", []))
    if "response_panel" in control_groups:
        signals.extend(["response_rendered", "stream_complete"])
    return _dedupe(signals)


def _controls_from_groups(control_groups: Dict[str, Any]) -> (List[str], List[str]):
    expected: List[str] = []
    required: List[str] = []
    for group in control_groups.values():
        labels = group.get("labels", [])
        if labels:
            expected.append(labels[0])
            if group.get("required"):
                required.append(labels[0])
    return _dedupe(expected), _dedupe(required)


def _structured_default_contract(title: str, url: str, content: str) -> Dict[str, Any]:
    page_type = _infer_page_type(url, title, content)
    control_groups = _infer_control_groups(url, title, content, page_type)
    expected_controls, required_controls = _controls_from_groups(control_groups)
    task_modes = _infer_task_modes(url, title, content, page_type, control_groups)
    transition_rules = _infer_transition_rules(page_type, task_modes, control_groups)
    completion_contracts = _infer_completion_contracts(page_type, task_modes)
    return {
        "summary_of_contents": _deterministic_summary(title, url, content),
        "page_type": page_type,
        "node_aliases": _infer_node_aliases(title, url, page_type),
        "goal_triggers": _infer_goal_triggers(task_modes, title, page_type),
        "control_groups": control_groups,
        "task_modes": task_modes,
        "transition_rules": transition_rules,
        "completion_contracts": completion_contracts,
        "evidence_zones": _infer_evidence_zones(page_type, control_groups),
        "verification_signals": _infer_verification_signals(control_groups, task_modes, completion_contracts, transition_rules),
        "expected_controls": expected_controls,
        "required_controls": required_controls,
        "terminal_capabilities": _infer_terminal_capabilities(url, page_type),
    }


def _sanitize_llm_contract(raw: Dict[str, Any]) -> Dict[str, Any]:
    allowed_keys = {
        "summary_of_contents",
        "page_type",
        "node_aliases",
        "goal_triggers",
        "control_groups",
        "task_modes",
        "completion_contracts",
        "verification_signals",
        "evidence_zones",
    }
    sanitized = {k: v for k, v in raw.items() if k in allowed_keys}
    if "node_aliases" in sanitized and not isinstance(sanitized["node_aliases"], list):
        sanitized.pop("node_aliases")
    if "goal_triggers" in sanitized and not isinstance(sanitized["goal_triggers"], dict):
        sanitized.pop("goal_triggers")
    return sanitized


def _llm_enrich_contract(title: str, url: str, content_snippet: str) -> Dict[str, Any]:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.warning("GROQ_API_KEY not set; skipping LLM enrichment")
        return {}

    try:
        import httpx

        prompt = f"""You are upgrading a site map node into an operational page contract.
Return strict JSON only. Use concise values. Do not wrap in markdown.

Required shape:
{{
  "summary_of_contents": "string",
  "page_type": "string",
  "node_aliases": ["alias"],
  "goal_triggers": {{"task_name": ["natural language trigger"]}},
  "control_groups": {{"group_name": {{"labels": ["..."], "zones": ["..."], "element_types": ["..."], "required": true}}}},
  "task_modes": {{"task_name": {{"task_type": "read_extract|create_action|form_fill|confirm_action|destructive_action|navigation"}}}},
  "completion_contracts": {{"task_name": {{"requires_all": ["..."], "requires_any": ["..."], "disallow": ["..."]}}}},
  "verification_signals": ["signal"],
  "evidence_zones": ["zone"]
}}

Rules:
- Treat the site map as ground truth for known-site behavior.
- Distinguish read/extract tasks from action tasks.
- Control groups must be deterministic families, not vague summaries.
- Completion contracts must define what counts as true success.

Page Title: {title}
URL: {url}
Content:
{content_snippet[:2000]}
"""

        payload = {
            "model": "openai/gpt-oss-20b",
            "messages": [
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_completion_tokens": 700,
            "response_format": {"type": "json_object"},
        }

        response = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20.0,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return _sanitize_llm_contract(parsed)
    except Exception as exc:
        logger.warning(f"LLM enrichment failed for '{title}': {exc}")
        return {}


def _build_node_contract(
    title: str,
    url: str,
    content: str,
    *,
    use_llm: bool,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    contract = _structured_default_contract(title, url, content)
    if use_llm:
        contract = _merge_dicts(contract, _llm_enrich_contract(title, url, content[:2000]))
    if overrides:
        contract = _merge_dicts(contract, overrides)

    control_groups = contract.get("control_groups", {}) or {}
    expected_controls, required_controls = _controls_from_groups(control_groups)
    if expected_controls:
        contract["expected_controls"] = expected_controls
    if required_controls:
        contract["required_controls"] = required_controls

    task_modes = contract.get("task_modes", {}) or {}
    generated_transition_rules = _infer_transition_rules(
        contract.get("page_type", ""), task_modes, control_groups
    )
    transition_rules = _merge_dicts(generated_transition_rules, contract.get("transition_rules", {}) or {})
    generated_completion_contracts = _infer_completion_contracts(
        contract.get("page_type", ""), task_modes
    )
    completion_contracts = _merge_dicts(generated_completion_contracts, contract.get("completion_contracts", {}) or {})

    contract["transition_rules"] = transition_rules
    contract["completion_contracts"] = completion_contracts
    contract["goal_triggers"] = contract.get("goal_triggers") or _infer_goal_triggers(
        task_modes, title, contract.get("page_type", "")
    )
    contract["node_aliases"] = _dedupe(contract.get("node_aliases", []))
    contract["terminal_capabilities"] = _dedupe(contract.get("terminal_capabilities", []))
    contract["expected_controls"] = _dedupe(contract.get("expected_controls", []))
    contract["required_controls"] = _dedupe(contract.get("required_controls", []))
    contract["verification_signals"] = _dedupe(
        contract.get("verification_signals", []) or _infer_verification_signals(
            control_groups, task_modes, completion_contracts, transition_rules
        )
    )
    contract["evidence_zones"] = _dedupe(contract.get("evidence_zones", []))
    return contract


def _site_node_from_contract(
    node_id: str,
    title: str,
    logical_path: str,
    url: str,
    content: str,
    *,
    path_regex: Optional[str] = None,
    use_llm: bool = False,
    overrides: Optional[Dict[str, Any]] = None,
) -> SiteNode:
    contract = _build_node_contract(title, url, content, use_llm=use_llm, overrides=overrides)
    return SiteNode(
        node_id=node_id,
        title=title,
        logical_path=logical_path,
        url=url,
        path_regex=path_regex if path_regex is not None else _url_to_path_regex(url),
        summary_of_contents=contract.get("summary_of_contents", ""),
        expected_controls=contract.get("expected_controls", []),
        required_controls=contract.get("required_controls", []),
        terminal_capabilities=contract.get("terminal_capabilities", []),
        page_type=contract.get("page_type", ""),
        node_aliases=contract.get("node_aliases", []),
        goal_triggers=contract.get("goal_triggers", {}),
        control_groups=contract.get("control_groups", {}),
        task_modes=contract.get("task_modes", {}),
        transition_rules=contract.get("transition_rules", {}),
        completion_contracts=contract.get("completion_contracts", {}),
        evidence_zones=contract.get("evidence_zones", []),
        verification_signals=contract.get("verification_signals", []),
    )


# ═══════════════════════════════════════════════════════════════════════
# Tree Builder
# ═══════════════════════════════════════════════════════════════════════


def build_tree_from_pillars(
    pillars: List[PillarSection],
    domain: str,
    use_llm: bool = False,
) -> SiteNode:
    root = _site_node_from_contract(
        "root",
        "Groq Platform",
        "root",
        f"https://{domain}/",
        "",
        path_regex=r"^/$",
        use_llm=use_llm,
        overrides={
            "page_type": "dashboard_overview",
            "summary_of_contents": "Entry point for Groq platform navigation across console, docs, playground, keys, dashboard, and settings.",
            "node_aliases": ["platform home", "groq platform"],
            "control_groups": {
                "global_navigation": _make_control_group(
                    ["Playground", "API Keys", "Dashboard", "Docs", "Settings", "Search"],
                    ["header"],
                    ["link", "button", "nav"],
                    required=True,
                )
            },
            "task_modes": {
                "navigate_platform": {
                    "task_type": "navigation",
                    "primary_cta_group": "global_navigation",
                    "success_evidence": ["target_page_loaded"],
                }
            },
            "evidence_zones": ["header", "main"],
            "terminal_capabilities": ["navigate_console", "navigate_docs"],
        },
    )

    marketing_pillars: List[PillarSection] = []
    console_pillars: List[PillarSection] = []
    for pillar in pillars:
        if "console.groq.com" in urlparse(pillar.url).netloc:
            console_pillars.append(pillar)
        else:
            marketing_pillars.append(pillar)

    console_node = _site_node_from_contract(
        "console_home",
        "GroqCloud Console Home",
        "root.console",
        "https://console.groq.com/home",
        "",
        path_regex=r"^/(home)?$",
        use_llm=use_llm,
        overrides={
            "page_type": "dashboard_overview",
            "node_aliases": ["console home", "groqcloud home"],
            "control_groups": {
                "console_navigation": _make_control_group(
                    ["Playground", "API Keys", "Dashboard", "Docs", "Settings"],
                    ["header", "left_sidebar"],
                    ["link", "button", "nav"],
                    required=True,
                ),
                "auth_entry": _make_control_group(
                    ["Login", "Sign in"],
                    ["header"],
                    ["button", "link"],
                ),
            },
            "task_modes": {
                "open_console_surface": {
                    "task_type": "navigation",
                    "primary_cta_group": "console_navigation",
                    "success_evidence": ["target_page_loaded"],
                }
            },
            "evidence_zones": ["header", "left_sidebar", "main"],
            "terminal_capabilities": ["navigate_console"],
        },
    )

    playground_node = _site_node_from_contract(
        "playground",
        "Playground",
        "root.console.playground",
        "https://console.groq.com/playground",
        "",
        path_regex=r"^/playground/?$",
        use_llm=use_llm,
    )

    api_keys_node = _site_node_from_contract(
        "api_keys",
        "API Keys",
        "root.console.api_keys",
        "https://console.groq.com/keys",
        "",
        path_regex=r"^/keys/?$",
        use_llm=use_llm,
    )

    dashboard_node = _site_node_from_contract(
        "dashboard_main",
        "Dashboard Overview",
        "root.console.dashboard",
        "https://console.groq.com/dashboard",
        "",
        path_regex=r"^/dashboard/?$",
        use_llm=use_llm,
    )

    usage_node = _site_node_from_contract(
        "usage_section",
        "Usage and Spend",
        "root.console.dashboard.usage",
        "https://console.groq.com/dashboard/usage",
        "",
        path_regex=r"^/dashboard/usage/?$",
        use_llm=use_llm,
    )

    logs_node = _site_node_from_contract(
        "logs_section",
        "Logs",
        "root.console.dashboard.logs",
        "https://console.groq.com/dashboard/logs",
        "",
        path_regex=r"^/dashboard/logs/?$",
        use_llm=use_llm,
    )

    batch_node = _site_node_from_contract(
        "batch_section",
        "Batch Jobs",
        "root.console.dashboard.batch",
        "https://console.groq.com/dashboard/batch",
        "",
        path_regex=r"^/dashboard/batch/?$",
        use_llm=use_llm,
    )

    dashboard_node.children = [usage_node, logs_node, batch_node]

    settings_node = _site_node_from_contract(
        "settings",
        "Settings",
        "root.console.settings",
        "https://console.groq.com/settings",
        "",
        path_regex=r"^/settings/?$",
        use_llm=use_llm,
    )

    docs_node = _site_node_from_contract(
        "docs_root",
        "Documentation",
        "root.console.docs",
        "https://console.groq.com/docs/overview",
        "",
        path_regex=r"^/docs(/overview)?/?$",
        use_llm=use_llm,
        overrides={
            "page_type": "docs_index",
            "node_aliases": ["docs home", "documentation hub"],
            "terminal_capabilities": ["read_documentation", "navigate_documentation"],
        },
    )
    docs_node.children = _build_docs_categories(console_pillars, use_llm)

    console_node.children = [playground_node, api_keys_node, dashboard_node, docs_node, settings_node]

    marketing_node = _site_node_from_contract(
        "groq_website",
        "Groq Website",
        "root.website",
        "https://groq.com/",
        "",
        path_regex=r"^/$",
        use_llm=use_llm,
        overrides={
            "page_type": "docs_index",
            "summary_of_contents": "Groq marketing site with product, pricing, architecture, privacy, and developer resource pages.",
            "terminal_capabilities": ["read_marketing_content", "navigate_marketing_site"],
            "control_groups": {
                "website_navigation": _make_control_group(
                    ["Platform Menu", "Solutions Menu", "Learn Menu", "Pricing Link", "Developers Menu", "Start Building Button"],
                    ["header"],
                    ["link", "button", "nav"],
                    required=True,
                )
            },
            "task_modes": {
                "navigate_marketing_site": {
                    "task_type": "navigation",
                    "primary_cta_group": "website_navigation",
                    "success_evidence": ["target_page_loaded"],
                }
            },
            "evidence_zones": ["header", "main"],
        },
    )

    for pillar in marketing_pillars:
        title = pillar.title.split(" - ")[0].strip()
        slug = _normalize_slug(title) or f"pillar_{pillar.pillar_num}"
        node = _site_node_from_contract(
            slug,
            title,
            f"root.website.{slug}",
            pillar.url,
            pillar.content,
            use_llm=use_llm,
        )
        marketing_node.children.append(node)

    root.children = [console_node, marketing_node]
    return root


def _build_docs_categories(console_pillars: List[PillarSection], use_llm: bool) -> List[SiteNode]:
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
            "children": [("docs_lora", "LoRA Inference", "/docs/lora")],
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
        {"id": "legal", "title": "Legal", "children": [("docs_legal", "Policies & Notices", "/docs/legal")]},
    ]

    pillar_by_path = {
        urlparse(pillar.url).path.rstrip("/"): pillar
        for pillar in console_pillars
    }

    nodes: List[SiteNode] = []
    for category in categories:
        category_node = _site_node_from_contract(
            category["id"],
            category["title"],
            f"root.console.docs.{category['id']}",
            f"https://console.groq.com/docs/{category['id']}",
            "",
            path_regex=rf"^/docs(?:/{category['id'].replace('_', '-')})?(?:/.*)?$",
            use_llm=use_llm,
            overrides={
                "page_type": "docs_index",
                "summary_of_contents": f"Documentation category for {category['title']} with {len(category['children'])} child pages.",
                "terminal_capabilities": ["read_documentation", "navigate_documentation"],
                "control_groups": {
                    "sidebar_navigation": _make_control_group(
                        ["Sidebar Navigation", category["title"]],
                        ["left_sidebar"],
                        ["nav", "link_list"],
                        required=True,
                    ),
                    "content_area": _make_control_group(
                        ["Content Area", category["title"]],
                        ["main"],
                        ["section", "article"],
                        required=True,
                    ),
                },
                "task_modes": {
                    "browse_category": {
                        "task_type": "navigation",
                        "primary_cta_group": "sidebar_navigation",
                        "success_evidence": ["child_doc_loaded"],
                    }
                },
                "evidence_zones": ["left_sidebar", "main"],
            },
        )

        for child_id, child_title, child_path in category["children"]:
            pillar = pillar_by_path.get(child_path.rstrip("/"))
            full_url = f"https://console.groq.com{child_path}"
            child_node = _site_node_from_contract(
                child_id,
                child_title,
                f"root.console.docs.{category['id']}.{child_id}",
                full_url,
                pillar.content if pillar else "",
                use_llm=use_llm,
            )
            category_node.children.append(child_node)
            if use_llm and pillar:
                time.sleep(0.1)

        nodes.append(category_node)
    return nodes


# ═══════════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════════


def _validate_path_regex(node: SiteNode, errors: List[str]) -> None:
    if node.node_id == "root":
        return
    if not node.path_regex:
        errors.append(f"{node.node_id}: missing path_regex")
        return
    if node.path_regex in {".*", "^.*$", "^/.+$"}:
        errors.append(f"{node.node_id}: path_regex too broad ({node.path_regex})")
    try:
        re.compile(node.path_regex)
    except re.error as exc:
        errors.append(f"{node.node_id}: invalid path_regex ({exc})")


def _validate_node_contract(node: SiteNode, parent: Optional[SiteNode], errors: List[str]) -> None:
    for field_name in ["node_id", "title", "logical_path", "url", "path_regex"]:
        if not getattr(node, field_name, "") and not (field_name == "path_regex" and node.node_id.startswith("docs_")):
            errors.append(f"{node.node_id or '<unknown>'}: missing {field_name}")

    if node.node_id != "root" and not node.page_type:
        errors.append(f"{node.node_id}: missing page_type")

    if parent and not node.logical_path.startswith(f"{parent.logical_path}."):
        errors.append(
            f"{node.node_id}: logical_path '{node.logical_path}' does not extend parent '{parent.logical_path}'"
        )

    if node.task_modes and not node.completion_contracts:
        errors.append(f"{node.node_id}: task_modes defined without completion_contracts")

    for task_name, steps in node.transition_rules.items():
        for step in steps:
            control_group = step.get("control_group")
            if control_group and control_group not in node.control_groups:
                errors.append(
                    f"{node.node_id}: transition '{task_name}' references missing control_group '{control_group}'"
                )

    for task_name in node.task_modes:
        if task_name not in node.completion_contracts:
            errors.append(f"{node.node_id}: missing completion contract for task '{task_name}'")

    _validate_path_regex(node, errors)

    for child in node.children:
        _validate_node_contract(child, node, errors)


def validate_site_map(root: SiteNode) -> None:
    errors: List[str] = []
    _validate_node_contract(root, None, errors)
    if errors:
        raise ValueError("Invalid site map contract:\n- " + "\n- ".join(errors))


def _count_nodes(node: SiteNode) -> int:
    return 1 + sum(_count_nodes(child) for child in node.children)


def _count_leaf_nodes(node: SiteNode) -> int:
    if not node.children:
        return 1
    return sum(_count_leaf_nodes(child) for child in node.children)


def _max_depth(node: SiteNode, depth: int = 0) -> int:
    if not node.children:
        return depth
    return max(_max_depth(child, depth + 1) for child in node.children)


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build an operational PageIndex (site_map.json) from groq.md"
    )
    parser.add_argument("--input", "-i", required=True, help="Path to the structured markdown file")
    parser.add_argument("--output", "-o", default="site_map.json", help="Output JSON file path")
    parser.add_argument("--domain", "-d", default="console.groq.com", help="Primary domain for the site map")
    parser.add_argument("--use-llm", action="store_true", help="Use gpt-oss-20b to enrich node contracts")
    args = parser.parse_args()

    logger.info(f"Reading input: {args.input}")
    with open(args.input, "r", encoding="utf-8") as handle:
        md_text = handle.read()

    pillars = parse_pillars(md_text)
    if not pillars:
        logger.error("No PILLAR sections found in input file")
        sys.exit(1)

    logger.info(f"Parsed {len(pillars)} pillars")
    root = build_tree_from_pillars(pillars, args.domain, use_llm=args.use_llm)
    validate_site_map(root)

    site_map = {
        "site_metadata": {
            "domain": args.domain,
            "version": "2.0.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_file": os.path.basename(args.input),
            "pillar_count": len(pillars),
            "contract_mode": "operational_page_contract",
        },
        "root": root.to_dict(),
    }

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(site_map, handle, indent=2, ensure_ascii=False)

    logger.info(f"Site map written to: {args.output}")
    logger.info(f"Total nodes: {_count_nodes(root)}")
    logger.info(f"Leaf nodes: {_count_leaf_nodes(root)}")
    logger.info(f"Max depth: {_max_depth(root)}")
    logger.info(f"File size: {os.path.getsize(args.output) / 1024:.1f} KB")


if __name__ == "__main__":
    main()

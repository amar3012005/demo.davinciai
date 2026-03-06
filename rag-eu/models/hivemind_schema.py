"""
HiveMind Universal Payload Schema v1.

All Qdrant points use FLAT payloads (no nesting) so that every field
is indexable. Factory functions enforce consistency while emitting
backward-compatible fields (e.g. `label`, `client_id`, `type`) so
legacy queries still match during migration.
"""

import time
import uuid as _uuid
from typing import Any, Dict, List, Literal, Optional

SCHEMA_VERSION = 1

DocType = Literal[
    "Website_Map",
    "Case_Memory",
    "Agent_Skill",
    "Agent_Rule",
    "General_KB",
    "Element_Context",
]


# ── Factory Functions ─────────────────────────────────────────────────────────

def _base(
    doc_type: DocType,
    domain: str,
    tenant_id: str,
    text: str,
    summary: str = "",
    **extra: Any,
) -> Dict[str, Any]:
    """Shared base payload builder."""
    agent_id = str(extra.pop("agent_id", "") or "unknown")
    session_type = str(extra.pop("session_type", "") or "unknown")
    payload: Dict[str, Any] = {
        # ── Universal fields ──
        "doc_type": doc_type,
        "domain": domain,
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "session_type": session_type,
        "text": text,
        "summary": summary,
        "created_at": int(time.time()),
        "uuid": str(_uuid.uuid4()),
        "schema_version": SCHEMA_VERSION,
    }
    payload.update(extra)
    return payload


def case_memory_payload(
    *,
    issue: str,
    solution: str,
    domain: str = "all",
    tenant_id: str = "tara",
    user_id: str = "unknown",
    issue_type: str = "general",
    severity: str = "standard",
    successful: bool = True,
) -> Dict[str, Any]:
    """Factory for Case Memory points."""
    clean_issue = (issue or "").strip()
    clean_solution = (solution or "").strip()
    narrative_issue = clean_issue
    if clean_issue and not clean_issue.endswith((".", "?", "!")):
        narrative_issue = f"{clean_issue}."
    narrative_text = f"A user asked: {narrative_issue}" if narrative_issue else "A user asked a question."

    return _base(
        doc_type="Case_Memory",
        domain=domain,
        tenant_id=tenant_id,
        text=narrative_text,
        summary=clean_solution,
        # ── Type-specific ──
        user_id=user_id,
        issue_type=issue_type,
        severity=severity,
        successful=successful,
        # ── Backward compat ──
        issue=clean_issue,
        solution=clean_solution,
    )


def agent_skill_payload(
    *,
    text: str,
    topic: str = "general",
    tenant_id: str = "tara",
    domain: str = "all",
) -> Dict[str, Any]:
    """Factory for Agent Skill points."""
    return _base(
        doc_type="Agent_Skill",
        domain=domain,
        tenant_id=tenant_id,
        text=text,
        summary=text,
        # ── Type-specific ──
        topic=topic,
        # ── Backward compat ──
        type="agent_skill",
    )


def agent_rule_payload(
    *,
    text: str,
    topic: str = "general",
    severity: str = "standard",
    tenant_id: str = "tara",
    domain: str = "all",
) -> Dict[str, Any]:
    """Factory for Agent Rule points."""
    return _base(
        doc_type="Agent_Rule",
        domain=domain,
        tenant_id=tenant_id,
        text=text,
        summary=text,
        # ── Type-specific ──
        topic=topic,
        severity=severity,
        # ── Backward compat ──
        type="agent_rule",
    )


def website_map_payload(
    *,
    url: str,
    concept: str,
    domain: str,
    tenant_id: str = "tara",
    key_selectors: Optional[List[str]] = None,
    action_script: str = "",
) -> Dict[str, Any]:
    """Factory for Website Map (Sitemap) points."""
    return _base(
        doc_type="Website_Map",
        domain=domain,
        tenant_id=tenant_id,
        text=concept,
        summary=f"Navigate to {url}",
        # ── Type-specific ──
        url=url,
        key_selectors=key_selectors or [],
        action_script=action_script,
        # ── Backward compat ──
        label="website_sitemap",
        client_id=tenant_id,
        concept=concept,
    )


def element_context_payload(
    *,
    context: str,
    url: str,
    domain: str,
    tenant_id: str = "tara",
    element_type: str = "",
    tara_id: str = "",
) -> Dict[str, Any]:
    """Factory for Element Context points (per-element guidance for Tara)."""
    return _base(
        doc_type="Element_Context",
        domain=domain,
        tenant_id=tenant_id,
        text=context,
        summary=context,
        # ── Type-specific ──
        url=url,
        element_type=element_type,
        tara_id=tara_id,
        # ── Backward compat ──
        label="element_context",
        client_id=tenant_id,
        context=context,
    )


def general_kb_payload(
    *,
    text: str,
    filename: str = "",
    doc_type_detail: str = "General",
    tenant_id: str = "tara",
    domain: str = "all",
    chunk_index: int = 0,
    doc_id: str = "",
    topics: str = "",
) -> Dict[str, Any]:
    """Factory for General KB / uploaded document chunks."""
    return _base(
        doc_type="General_KB",
        domain=domain,
        tenant_id=tenant_id,
        text=text,
        summary=text[:200],
        # ── Type-specific ──
        filename=filename,
        original_filename=filename,
        doc_type_detail=doc_type_detail,
        chunk_index=chunk_index,
        doc_id=doc_id,
        topics=topics,
        # ── Backward compat ──
        data_type="Internal_KB",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def read_text(payload: Dict[str, Any]) -> str:
    """Read primary text from any schema version, with fallback."""
    return payload.get("text") or payload.get("issue") or payload.get("concept") or payload.get("context") or ""


def read_summary(payload: Dict[str, Any]) -> str:
    """Read summary/solution from any schema version, with fallback."""
    return payload.get("summary") or payload.get("solution") or payload.get("text") or ""


def read_doc_type(payload: Dict[str, Any]) -> str:
    """Read doc_type with fallback to legacy fields."""
    if payload.get("doc_type"):
        return payload["doc_type"]
    # Legacy detection
    if payload.get("label") == "website_sitemap":
        return "Website_Map"
    if payload.get("label") == "element_context":
        return "Element_Context"
    t = payload.get("type", "")
    if t == "agent_skill":
        return "Agent_Skill"
    if t == "agent_rule":
        return "Agent_Rule"
    if payload.get("data_type") == "Internal_KB":
        return "General_KB"
    if payload.get("issue") and payload.get("solution"):
        return "Case_Memory"
    return "Unknown"


def read_created_at(payload: Dict[str, Any]) -> int:
    """Read creation timestamp with fallback."""
    return payload.get("created_at") or payload.get("timestamp") or 0

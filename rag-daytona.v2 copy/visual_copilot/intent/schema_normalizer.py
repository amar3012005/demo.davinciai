from dataclasses import replace
from urllib.parse import urlparse
from typing import Any


def _extract_host(url: str) -> str:
    if not url or "://" not in url:
        return ""
    try:
        return (urlparse(url).netloc or "").replace("www.", "")
    except Exception:
        return ""


def normalize_schema_domain(schema: Any, current_url: str) -> Any:
    host = _extract_host(current_url)
    if not host:
        return schema
    current_domain = getattr(schema, "domain", "") or ""
    if current_domain == host:
        return schema
    try:
        return replace(schema, domain=host)
    except Exception:
        return schema

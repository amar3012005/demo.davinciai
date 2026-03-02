from typing import Any

from visual_copilot.text.tokenization import _tokenize


def lexical_score(node: Any, query: str) -> float:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return 0.0
    text_tokens = _tokenize((getattr(node, "text", "") or "") + " " + (getattr(node, "id", "") or ""))
    if not text_tokens:
        return 0.0
    overlap = len(query_tokens & text_tokens)
    return min(1.0, overlap / max(1, len(query_tokens)))

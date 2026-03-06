from typing import Any, Iterable, List


def top_candidates(candidates: Iterable[Any], *, limit: int = 5) -> List[Any]:
    ordered = sorted(candidates, key=lambda c: float(getattr(c, "final_score", 0.0) or 0.0), reverse=True)
    return ordered[:limit]

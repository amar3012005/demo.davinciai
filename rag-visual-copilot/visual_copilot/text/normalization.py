from typing import List
from visual_copilot.constants import _DOMAIN_LABEL_SYNONYMS
from visual_copilot.text.tokenization import _canonicalize_label

def _expand_label_synonyms(label: str, domain: str) -> List[str]:
    canonical = _canonicalize_label(label)
    if not canonical:
        return []
    variants = {canonical}
    domain_key = (domain or "").lower().replace("www.", "")
    for base_domain, mapping in _DOMAIN_LABEL_SYNONYMS.items():
        if domain_key.endswith(base_domain):
            for src, targets in mapping.items():
                src_can = _canonicalize_label(src)
                if src_can == canonical:
                    variants.update(_canonicalize_label(t) for t in targets)
                for t in targets:
                    if _canonicalize_label(t) == canonical:
                        variants.add(src_can)
    return sorted(v for v in variants if v)


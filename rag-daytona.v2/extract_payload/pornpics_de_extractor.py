import json
import os
import re
from urllib.parse import urlparse


INPUT_FILE = "/Users/amar/demo.davinciai/rag-daytona.v2/Visual-co-plan/pornpics_de.md"
OUTPUT_FILE = "/Users/amar/demo.davinciai/rag-daytona.v2/extract_payload/pornpics_de_extracted_hivemind.json"


def normalize_domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().replace("www.", "") or "pornpics.de"
    except Exception:
        return "pornpics.de"


def selector_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path == "/":
        return "a[href='/']"
    return f"a[href*='{path}']"


def clean_label(label: str) -> str:
    s = label.strip()
    s = re.sub(r"!\[Image.*", "", s).strip()
    s = re.sub(r"\[[^\]]*\]", "", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def infer_zone(label: str, href: str) -> str:
    s = f"{label} {href}".lower()
    if any(k in s for k in ["login", "signup", "favorites", "most popular", "most recent", "top rated", "tags", "pornstars", "videos"]):
        return "nav"
    return "main"


def strategy_for_url(url: str, domain: str) -> dict:
    path = (urlparse(url).path or "/").lower()
    if "/popular" in path:
        concept = "Browse most popular galleries"
        sequence = ["Open Pics menu", "Click Most Popular", "Open gallery"]
        action = "extraction"
    elif "/recent" in path:
        concept = "Browse most recent galleries"
        sequence = ["Open Pics menu", "Click Most Recent", "Open gallery"]
        action = "extraction"
    elif "/rating" in path:
        concept = "Browse top rated galleries"
        sequence = ["Open Pics menu", "Click Top Rated", "Open gallery"]
        action = "extraction"
    elif "/tags" in path:
        concept = "Browse tag catalog and open tag results"
        sequence = ["Open Tags", "Select tag", "Open gallery"]
        action = "navigation"
    elif "/pornstars" in path:
        concept = "Browse pornstar directory and open profile galleries"
        sequence = ["Open Pornstars", "Select pornstar", "Open gallery"]
        action = "search"
    elif "/videos" in path:
        concept = "Navigate to videos section"
        sequence = ["Open Videos", "Select video", "Review results"]
        action = "navigation"
    else:
        concept = "Search or browse categories from homepage"
        sequence = ["Open homepage", "Use search or category links", "Open gallery"]
        action = "search"

    return {
        "doc_type": "Strategy_Sequence",
        "domain": domain,
        "action": action,
        "sequence": sequence,
        "constraints_order": ["entity", "tag", "sort"],
        "blocking_rules": {"open_gallery": ["entity"]},
        "example_url": url,
        "text": f"Strategy for {action} on {domain}: {concept}. URL: {url}",
        "concept": concept
    }


def should_skip(label: str, href: str) -> bool:
    if not label:
        return True
    if len(label) < 2:
        return True
    if len(label) > 80:
        return True
    if re.fullmatch(r"[\W_]+", label):
        return True
    if re.fullmatch(r"\d+", label):
        return True

    h = href.lower()
    p = (urlparse(href).path or "").lower()
    ll = label.lower()

    if any(h.startswith(s) for s in ["mailto:", "tel:", "javascript:"]):
        return True
    if any(ext in p for ext in [".jpg", ".jpeg", ".png", ".svg", ".webp", ".gif", ".ico", ".woff", ".woff2"]):
        return True
    if any(k in h for k in ["static.pornpics", "cdni.pornpics", "/api/user/logout", "/api/user/gauth_start"]):
        return True
    if any(k in ll for k in ["image ", "google", "dark theme", "light theme", "skip to content"]):
        return True
    # Drop non-actionable boilerplate
    if any(k in ll for k in [
        "privacy", "terms", "dmca", "support", "contact", "cookies", "copyright",
        "about us", "all rights reserved", "feedback", "newsletter", "share", "report"
    ]):
        return True
    # Drop likely junk labels with no retrieval value
    if ll in {"menu", "home", "back", "next", "prev", "close", "open"}:
        return True
    return False


def extract():
    if not os.path.exists(INPUT_FILE):
        raise FileNotFoundError(f"Input file not found: {INPUT_FILE}")

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    pillar_pattern = re.compile(r"^## PILLAR \d+:\s*(https?://\S+)", re.MULTILINE)
    matches = list(pillar_pattern.finditer(content))

    pillars = []
    for i, m in enumerate(matches):
        url = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()
        title_match = re.search(r"^Title:\s*(.+)", body, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else url
        pillars.append({"url": url, "title": title, "body": body})

    output = []
    seen_strategy = set()
    seen_hint = set()

    for pillar in pillars:
        p_url = pillar["url"]
        p_body = pillar["body"]
        p_domain = normalize_domain(p_url)

        s = strategy_for_url(p_url, p_domain)
        skey = f"{p_domain}:{s['action']}:{p_url}"
        if skey not in seen_strategy:
            seen_strategy.add(skey)
            output.append(s)

        output.append({
            "doc_type": "Website_Map",
            "domain": p_domain,
            "url": p_url,
            "concept": s["concept"],
            "sequence": s["sequence"],
            "blocking_rules": ["wait_for_results_render"],
            "action_script": "await page.goto('<url>'); // search/filter and open gallery"
        })

        links = re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)[^)]*\)", p_body)
        for raw_label, href in links:
            label = clean_label(raw_label)
            if should_skip(label, href):
                continue

            d = normalize_domain(href)
            if not (d.endswith("pornpics.de") or d.endswith("pornpics.com")):
                continue

            sel = selector_from_url(href)
            key = f"{d}:{sel}:{label.lower()}"
            if key in seen_hint:
                continue
            seen_hint.add(key)

            output.append({
                "doc_type": "Visual_Hint",
                "domain": d,
                "selector": sel,
                "keyword_match": label.lower(),
                "element_type": "a",
                "text_pattern": label,
                "zone": infer_zone(label, href),
                "description": f"Navigate to '{label}' ({href})"
            })

            # Keep output manageable and higher precision for embedding/push.
            if len(seen_hint) >= 300:
                break
        if len(seen_hint) >= 300:
            break

    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        json.dump(output, out, indent=2, ensure_ascii=False)

    print(f"✅ Extracted {len(output)} records to {OUTPUT_FILE}")


if __name__ == "__main__":
    extract()

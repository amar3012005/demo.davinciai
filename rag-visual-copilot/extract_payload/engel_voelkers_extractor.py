import json
import os
import re
from urllib.parse import urlparse


INPUT_FILE = "/Users/amar/demo.davinciai/rag-daytona.v2/Visual-co-plan/engel_voelkers.md"
FALLBACK_INPUT = "/Users/amar/demo.davinciai/rag-daytona.v2/Engel & Völkers.md"
OUTPUT_FILE = "/Users/amar/demo.davinciai/rag-daytona.v2/extract_payload/engelvoelkers_extracted_hivemind.json"


def pick_input_file() -> str:
    if os.path.exists(INPUT_FILE):
        return INPUT_FILE
    return FALLBACK_INPUT


def normalize_domain(url: str) -> str:
    try:
        host = (urlparse(url).netloc or "").lower().replace("www.", "")
        return host or "engelvoelkers.com"
    except Exception:
        return "engelvoelkers.com"


def selector_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path == "/":
        return "a[href='/'], a[href*='engelvoelkers.com']"
    return f"a[href*='{path}']"


def infer_zone(text: str, href: str) -> str:
    s = f"{text} {href}".lower()
    if any(k in s for k in ["kontakt", "impressum", "datenschutz", "newsletter"]):
        return "footer"
    if any(k in s for k in ["kaufen", "mieten", "verkaufen", "finanzierung", "immobilien"]):
        return "main"
    return "nav"


def infer_action(text: str, href: str) -> str:
    s = f"{text} {href}".lower()
    if any(k in s for k in ["kaufen", "mieten", "immobilie", "marktbericht", "gg", "lifestyle"]):
        return "extraction"
    if any(k in s for k in ["kontakt", "anfrage", "jetzt kontaktieren", "makler:in werden"]):
        return "interaction"
    return "navigation"


def create_strategy(url: str, title: str, domain: str) -> dict:
    low = f"{title} {url}".lower()
    action = infer_action(title, url)
    if "kaufen" in low:
        concept = "Find property listings to buy"
        sequence = ["Open Kaufen & Mieten", "Select Kaufen", "Apply region/type filters", "Open matching listing"]
    elif "mieten" in low:
        concept = "Find property listings to rent"
        sequence = ["Open Kaufen & Mieten", "Select Mieten", "Apply region/type filters", "Open matching listing"]
    elif "finanzierung" in low:
        concept = "Navigate to financing information"
        sequence = ["Open financing section", "Review financing options", "Open contact path"]
    elif "immobilienbewertung" in low:
        concept = "Navigate to property valuation flow"
        sequence = ["Open valuation page", "Enter property details", "Submit valuation request"]
    else:
        concept = "Navigate Engel & Völkers real estate sections"
        sequence = ["Open homepage", "Choose Kaufen or Mieten", "Open relevant region page", "Review listing details"]

    return {
        "doc_type": "Strategy_Sequence",
        "domain": domain,
        "action": action,
        "sequence": sequence,
        "constraints_order": ["transaction_type", "location", "property_type", "budget"],
        "blocking_rules": {
            "open_listing": ["transaction_type", "location"]
        },
        "example_url": url,
        "text": f"Strategy for {action} on {domain}: {concept}. URL: {url}",
        "concept": concept
    }


def extract():
    file_path = pick_input_file()
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Input file not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    pillar_pattern = re.compile(r"^## PILLAR \d+:\s*(https?://\S+)", re.MULTILINE)
    matches = list(pillar_pattern.finditer(content))

    pillars = []
    for i, match in enumerate(matches):
        url = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()
        title_match = re.search(r"^Title:\s*(.+)", body, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else url
        pillars.append({"url": url, "title": title, "body": body})

    if not pillars:
        # fallback: use SOURCE URLs from markdown if no explicit pillars
        source_urls = re.findall(r"URL Source:\s*(https?://\S+)", content)
        for u in source_urls:
            pillars.append({"url": u, "title": u, "body": content})

    output = []
    seen_hint = set()
    seen_strategy = set()

    for pillar in pillars:
        p_url = pillar["url"]
        p_title = pillar["title"]
        p_body = pillar["body"]
        p_domain = normalize_domain(p_url)

        # Strategy per pillar
        strategy = create_strategy(p_url, p_title, p_domain)
        skey = f"{p_domain}:{strategy['action']}:{p_url}"
        if skey not in seen_strategy:
            seen_strategy.add(skey)
            output.append(strategy)

        # Optional map entry to keep compatibility with older flows
        output.append({
            "doc_type": "Website_Map",
            "domain": p_domain,
            "url": p_url,
            "concept": strategy.get("concept", p_title),
            "sequence": strategy.get("sequence", []),
            "blocking_rules": ["wait_for_results_after_filter", "ensure_listing_cards_visible"],
            "action_script": "await page.goto('<url>'); // choose kaufen/mieten and open listing"
        })

        # Visual hints from markdown links
        links = re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", p_body)
        for text, href in links:
            label = re.sub(r"\s+", " ", text).strip()
            if not label:
                continue
            if label.lower().startswith("image "):
                continue
            if len(label) > 90:
                continue
            if any(skip in label.lower() for skip in ["cookie", "privacy", "datenschutz", "impressum"]):
                continue

            domain = normalize_domain(href)
            parsed_href = urlparse(href)
            path = (parsed_href.path or "").lower()
            if any(path.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".svg", ".webp", ".gif", ".ico", ".woff", ".woff2"]):
                continue
            if "/_next/image" in path:
                continue
            if any(skip in href.lower() for skip in ["storyblok.com", "website.engelvoelkers.com/_next"]):
                continue

            selector = selector_from_url(href)
            zone = infer_zone(label, href)

            hkey = f"{domain}:{selector}:{label.lower()}"
            if hkey in seen_hint:
                continue
            seen_hint.add(hkey)

            output.append({
                "doc_type": "Visual_Hint",
                "domain": domain,
                "selector": selector,
                "keyword_match": label.lower(),
                "element_type": "a",
                "text_pattern": label,
                "zone": zone,
                "description": f"Navigate to '{label}' ({href})"
            })

    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        json.dump(output, out, indent=2, ensure_ascii=False)

    print(f"✅ Extracted {len(output)} records to {OUTPUT_FILE}")


if __name__ == "__main__":
    extract()

import json
import re

md_file = "/Users/amar/demo.davinciai/rag-daytona.v2/Visual-co-plan/console_groq.md"
json_file = "/Users/amar/demo.davinciai/rag-daytona.v2/extract_payload/console_groq_extracted_hivemind.json"

with open(md_file, "r") as f:
    text = f.read()

# Find all Markdown links pointing to console.groq.com
pattern = re.compile(r'\[([^\]]+)\]\((https://console\.groq\.com/[^)]+)\)')
matches = pattern.findall(text)

try:
    with open(json_file, "r") as f:
        data = json.load(f)
except FileNotFoundError:
    data = []

existing_urls = set()
for d in data:
    if d.get("doc_type") == "Visual_Hint":
        kw = d.get("keyword_match", "")
        existing_urls.add(kw)

added = 0
for text_pattern, url in matches:
    text_pattern = text_pattern.strip()
    text_pattern = re.sub(r'[\n\r]+', ' ', text_pattern)
    
    # filter out long chunks of text and empty ones
    if not text_pattern or len(text_pattern) > 80:
        continue
        
    path = url.replace("https://console.groq.com", "")
    if not path.startswith("/"):
        path = "/" + path
        
    if path not in existing_urls:
        hint = {
            "doc_type": "Visual_Hint",
            "selector": f"a[href*='{path}']",
            "keyword_match": path,
            "element_type": "a",
            "text_pattern": text_pattern,
            "zone": "nav" if "/docs" in path else "main",
            "description": f"Navigation link to {text_pattern}"
        }
        data.append(hint)
        existing_urls.add(path)
        added += 1

with open(json_file, "w") as f:
    json.dump(data, f, indent=2)

print(f"Algorithm successfully added {added} detailed visual hints to the Hivemind!")

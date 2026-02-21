import os
import json
import re
from groq import Groq
from urllib.parse import urlparse

# Ensure you have your GROQ_API_KEY set in your environment
# export GROQ_API_KEY="your-api-key"

client = Groq()

SYSTEM_PROMPT = """
You are the Groq.com Hivemind Compiler. Your mission is to perform a LOSSLESS extraction of Groq's structural map and visual targets from raw markdown.

### OPERATIONAL CONSTRAINTS:
1. DO NOT SUMMARIZE: Every distinct URL or functional link in the markdown must be represented in the 'visual_hints' array.
2. ISOLATE DOMAIN: Extract the base domain accurately from the provided Source URL.
3. BE SURGICAL: Generate 'key_selectors' using precise text-matching patterns (e.g., "a:has-text('Pricing')") or attribute filters (e.g., "a[href*='/pricing']").
4. HIERARCHY: Map the 'Strategy_Sequence' based on the logical flow from the Home page to the terminal goal (e.g., Home -> Navigation -> Pricing Page -> Select Tier).

### SOURCE CONTEXT:
- Source URL: {source_url}
- Page Title: {page_title}
- Target Domain: {domain}

### REQUIRED JSON STRUCTURE EXAMPLE:
{{
  "strategy_sequence": {{
    "doc_type": "Website_Map",
    "domain": "groq.com",
    "url": "https://groq.com/pricing",
    "concept": "Navigate to and evaluate inference pricing",
    "sequence": ["Locate Navigation", "Select Pricing Link", "Verify Tiers"],
    "blocking_rules": ["Do not select pricing until navigation is located", "Do not verify tiers before pricing page loaded"],
    "action_script": "await page.goto('{source_url}'); await page.click('text=Pricing');"
  }},
  "visual_hints": [
    {{
      "doc_type": "Visual_Hint",
      "selector": "a[href*='/pricing']",
      "keyword_match": "/pricing",
      "element_type": "a",
      "text_pattern": "See Pricing",
      "zone": "nav",
      "description": "Navigation link to pricing page"
    }}
  ]
}}
"""

def extract_domain(url):
    try:
        parsed = urlparse(url)
        return parsed.netloc or "groq.com"
    except:
        return "groq.com"

def extract_hivemind_data(markdown_content: str, source_url: str, page_title: str):
    """
    Sends the markdown content to Groq and extracts matching JSON payloads.
    """
    domain = extract_domain(source_url)
    
    user_content = f"""
Extract everything from this markdown:
---
{markdown_content}
---
"""
    try:
        response = client.chat.completions.create(
            # llama-3.3-70b-versatile is a high-reasoning model recommended for extraction
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT.format(
                    source_url=source_url, 
                    page_title=page_title, 
                    domain=domain
                )},
                {"role": "user", "content": user_content},
            ],
            response_format={
                "type": "json_object"
            },
            # Adjust max tokens if needed depending on output size
            max_tokens=8000,
            temperature=0.1
        )
        result_json = response.choices[0].message.content
        return json.loads(result_json)
    except Exception as e:
        print(f"Error during Groq extraction: {e}")
        return None

def main():
    # specifically reading from Visual-co-plan the groq.md
    file_path = "/Users/amar/demo.davinciai/rag-daytona.v2/Visual-co-plan/groq.md"
    
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Split by PILLAR headers
    pillar_pattern = re.compile(r'^## PILLAR \d+:\s*(https?://\S+)', re.MULTILINE)
    matches = list(pillar_pattern.finditer(content))
    
    pillars = []
    for i, match in enumerate(matches):
        url = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()

        # Extract title (first line that looks like a title)
        title_match = re.search(r'^Title:\s*(.+)', body, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else url

        pillars.append({
            "url": url,
            "title": title,
            "body": body
        })

    print(f"Found {len(pillars)} Pillars in the document.")

    all_results = []
    
    for i, pillar in enumerate(pillars):
        print(f"Processing Pillar {i+1}/{len(pillars)} (URL: {pillar['url']})...")
        pillar_text = pillar['body'][:50000] 
        result = extract_hivemind_data(pillar_text, pillar['url'], pillar['title'])
        if result:
            all_results.append(result)
            print(f"✓ Successfully extracted data for Pillar {i+1}")
        else:
            print(f"✗ Failed to extract data for Pillar {i+1}")
            
    # Ultimate Requirement: Isolate Strategy from Hints (Flat List)
    flat_results = []
    for item in all_results:
        # Append Strategy Sequence
        if "strategy_sequence" in item:
            flat_results.append(item["strategy_sequence"])
        # Append all individual Visual Hints
        if "visual_hints" in item:
            flat_results.extend(item["visual_hints"])

    # Output to unified JSON file in extract_payload
    output_file = "/Users/amar/demo.davinciai/rag-daytona.v2/extract_payload/groq_extracted_hivemind.json"
    with open(output_file, "w", encoding="utf-8") as out:
        json.dump(flat_results, out, indent=2)
        
    print(f"\nExtraction complete! Separated (Flattened) Results saved to {output_file}")

if __name__ == "__main__":
    main()

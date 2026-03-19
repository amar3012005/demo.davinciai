import json
import re

with open('/Users/amar/demo.davinciai/rag-daytona.v2/Visual-co-plan/console_groq.md', 'r') as f:
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
    
    # Very basic static parsing to generate much more comprehensive payload for each pillar
    print(f"URL: {url}, Length: {len(body)}")
    

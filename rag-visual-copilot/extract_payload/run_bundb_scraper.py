import urllib.request
import time
import os

BUNDB_PILLARS = [
    "https://bundb.de/",
    "https://bundb.de/agentur",
    "https://bundb.de/leistungen",
    "https://bundb.de/referenzen",
    "https://bundb.de/blog",
    "https://bundb.de/kontakt",
    "https://bundb.de/impressum",
    "https://bundb.de/datenschutz",
    "https://bundb.de/karriere"
]

output_file = "/Users/amar/demo.davinciai/rag-daytona.v2/Visual-co-plan/bundb_guide.md"
os.makedirs(os.path.dirname(output_file), exist_ok=True)

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
}

with open(output_file, "w", encoding="utf-8") as f:
    f.write("# B&B. Markenagentur: Master Structural & Strategic Guide\n\n")
    f.write(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    
    for i, url in enumerate(BUNDB_PILLARS, 1):
        jina_url = f"https://r.jina.ai/{url}"
        print(f"📥 Mapping B&B Pillar {i}/{len(BUNDB_PILLARS)}: {url}")
        
        try:
            req = urllib.request.Request(jina_url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status == 200:
                    content = response.read().decode('utf-8')
                    f.write(f"\n\n---\n## PILLAR {i}: {url}\n\n")
                    f.write(content)
                else:
                    print(f"⚠️ Warning: {url} returned status {response.status}")
            time.sleep(2) # rate limits
        except Exception as e:
            print(f"❌ Failed: {url} - {e}")

print(f"✅ Ground Truth file '{output_file}' is ready.")

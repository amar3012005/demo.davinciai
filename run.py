import requests
import time

# The 10 Primary Pillars for Da'Vinci AI
DAVINCI_PILLARS = [
    "https://davinciai.eu/",
]

# Note: Subdomains like dashboard.davinciai.eu may require 
# Method 2 (Playwright) if they are behind your login wall.

with open("DaVinci_Master_Guide.md", "w", encoding="utf-8") as f:
    f.write("# Da'Vinci AI: Master Ecosystem Guide\n\n")
    f.write("## CORE ARCHITECTURE: Modular Multi-Agentic RAG (MMAR)\n\n")
    
    for i, url in enumerate(DAVINCI_PILLARS, 1):
        # Using Jina Reader to ensure clean formatting for your RAG pipeline
        jina_url = f"https://r.jina.ai/{url}"
        print(f"📥 Mapping Da'Vinci Pillar {i}/{len(DAVINCI_PILLARS)}: {url}")
        
        try:
            time.sleep(1) # Be gentle with your own server!
            res = requests.get(jina_url, timeout=30)
            if res.status_code == 200:
                f.write(f"\n\n---\n## PILLAR {i}: {url}\n\n")
                f.write(res.text)
        except Exception as e:
            print(f"❌ Failed: {url} - {e}")

print("✅ Ground Truth file 'DaVinci_Master_Guide.md' is ready for TARA_x1.")
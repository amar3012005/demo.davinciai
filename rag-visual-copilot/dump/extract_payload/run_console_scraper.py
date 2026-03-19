import requests
import time

CONSOLE_PILLARS = [
    "https://console.groq.com/",
    "https://console.groq.com/docs/overview",
    "https://console.groq.com/docs/quickstart",
    "https://console.groq.com/docs/models",
    "https://console.groq.com/docs/api-reference",
    "https://console.groq.com/docs/vision",
    "https://console.groq.com/docs/rate-limits",
    "https://console.groq.com/playground",
    "https://console.groq.com/keys",
    "https://console.groq.com/dashboard",
    "https://console.groq.com/settings"
]

with open("/Users/amar/demo.davinciai/rag-daytona.v2/Visual-co-plan/console_groq.md", "w", encoding="utf-8") as f:
    f.write("# Groq Console: Master Ecosystem Guide\n\n")
    
    for i, url in enumerate(CONSOLE_PILLARS, 1):
        jina_url = f"https://r.jina.ai/{url}"
        print(f"📥 Mapping Console Pillar {i}/{len(CONSOLE_PILLARS)}: {url}")
        
        try:
            time.sleep(2) # rate limits
            res = requests.get(jina_url, timeout=30)
            if res.status_code == 200:
                f.write(f"\n\n---\n## PILLAR {i}: {url}\n\n")
                f.write(res.text)
        except Exception as e:
            print(f"❌ Failed: {url} - {e}")

print("✅ Ground Truth file 'console_groq.md' is ready.")

import time
import urllib.request

from urls import ENGEL_VOELKERS_DE


OUTPUT_FILE = "/Users/amar/demo.davinciai/rag-daytona.v2/Visual-co-plan/engel_voelkers.md"


def main():
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("# Engel & Völkers Germany: Master Structural Guide\n\n")

        for i, url in enumerate(ENGEL_VOELKERS_DE, 1):
            jina_url = f"https://r.jina.ai/{url}"
            print(f"📥 Mapping Engel & Völkers Pillar {i}/{len(ENGEL_VOELKERS_DE)}: {url}")

            try:
                time.sleep(2)
                req = urllib.request.Request(jina_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8", errors="ignore")
                    f.write(f"\n\n---\n## PILLAR {i}: {url}\n\n")
                    f.write(body)
            except Exception as e:
                print(f"❌ Failed: {url} - {e}")

    print(f"✅ Ground Truth file '{OUTPUT_FILE}' is ready.")


if __name__ == "__main__":
    main()

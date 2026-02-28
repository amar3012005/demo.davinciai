import asyncio
import httpx

async def get_domains():
    async with httpx.AsyncClient() as client:
        resp = await client.post("https://878ba705-a90e-44d7-96fc-782399dd97ae.europe-west3-0.gcp.cloud.qdrant.io:6333/collections/tara_hive/points/query", json={
            "filter": None,
            "limit": 100,
            "with_payload": True,
            "with_vector": False
        })
        data = resp.json()
        points = data.get("result", [])
        domains = set([p["payload"].get("domain") for p in points if "payload" in p])
        print("DOMAINS IN TARA_HIVE:")
        for d in domains:
            print("-", d)

asyncio.run(get_domains())

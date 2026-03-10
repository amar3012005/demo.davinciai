import asyncio
import aiohttp

async def main():
    async with aiohttp.ClientSession() as session:
        mock_report = {"message": "test"}
        url = "https://api.enterprise.davinciai.eu:8450/api/webhooks/session"
        async with session.post(url, json=mock_report) as resp:
            print(f"Status: {resp.status}")
            print(f"Text: {await resp.text()}")

asyncio.run(main())

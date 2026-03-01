import asyncio
import websockets
import json
import base64
import os

api_key = os.getenv("CARTESIA_API_KEY", "your-key")

async def test():
    ws_url = f"wss://api.cartesia.ai/tts/websocket?api_key={api_key}&cartesia_version=2024-06-10"
    try:
        async with websockets.connect(ws_url) as ws:
            print("Connected")
            req = {
                "context_id": "test-123",
                "model_id": "sonic-3",
                "transcript": "<emotion value=\"enthusiastic\" /> hello world",
                "voice": {
                    "mode": "id",
                    "id": "07bc462a-c644-49f1-baf7-82d5599131be",
                    "__experimental_controls": {
                        "emotion": ["anger", "positivity", "surprise", "sadness", "curiosity"]
                    }
                },
                "output_format": {
                    "container": "raw",
                    "encoding": "pcm_f32le",
                    "sample_rate": 44100
                },
                "continue": False
            }
            await ws.send(json.dumps(req))
            
            while True:
                msg = await ws.recv()
                data = json.loads(msg)
                print(data)
                if data.get("done") or data.get("type") == "error" or data.get("error"):
                    break
    except Exception as e:
        print("Error:", e)

asyncio.run(test())

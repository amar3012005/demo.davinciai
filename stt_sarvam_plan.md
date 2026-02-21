Implementation Plan - Sarvam AI STT for Telugu
The goal is to build a Speech-to-Text (STT) microservice using Sarvam AI's real-time WebSocket API, specifically optimized for Telugu, replacing the functionality of stt_groq_whisper.

User Review Required
IMPORTANT

The provided Sarvam AI API endpoint (/speech-to-text-translate/ws) is described as providing English translation. If you require Telugu transcription (Telugu audio -> Telugu text), this might be the wrong endpoint. I will proceed with this endpoint as explicitly requested, which effectively makes this a "Speech-to-Text-Translate" service. Use model=saaras:v2.5 as requested.

Proposed Changes
[NEW] stt_sarvam Directory
I will create a new directory stt_sarvam with the following structure, mirroring stt_groq_whisper:

[NEW] stt_sarvam/config.py
Configuration class loading from environment variables.
Defaults:
SARVAM_API_KEY: (User must provide)
SARVAM_MODEL: saaras:v2.5
SAMPLE_RATE: 16000
PORT: 8003 (New port to avoid conflict)
[NEW] stt_sarvam/sarvam_client.py
SarvamClient class to handle WebSocket connection to wss://api.sarvam.ai/speech-to-text-translate/ws.
Handles bi-directional streaming:
Sends: Audio chunks (PCM S16LE).
Receives: JSON transcripts and events.
Maps Sarvam events (START_SPEECH, END_SPEECH) to match stt_groq_whisper behavior if necessary.
[NEW] stt_sarvam/app.py
FastAPI application.
Endpoint: WebSocket /api/v1/transcribe/stream.
Interface:
Accepts Raw PCM (16kHz, 16-bit, mono) or JSON config.
Returns JSON {"type": "data", "data": {"transcript": "...", "is_final": ...}}.
Orchestrates the SarvamClient session.
[NEW] stt_sarvam/requirements.txt
fastapi, uvicorn, 
websockets
, python-dotenv, numpy.
[NEW] stt_sarvam/static/client.html
A simple HTML/JS client to test the WebSocket stream independently, matching the request.
[MODIFY] orchestra_daytona.v2 Configuration
[MODIFY] 
orchestra_daytona.v2/config.yaml
Update STT service URL to point to the new service (if desired for integration testing).
Note: I will primarily provide the new service and let the user decide when to switch the main orchestrator config, but I will prepare the steps. stt_groq_whisper was on 8002, this will be on 8003.
Verification Plan
Automated Tests
None planned for this phase (manual verification preferred for audio/websocket).
Manual Verification
Start the Service:
bash
cd stt_sarvam
export SARVAM_API_KEY="<your-api-key>"
uvicorn app:app --port 8003
Test Client:
Open http://localhost:8003/client in a browser.
Click "Start Recording".
Speak in Telugu.
Verify that English translation (or transcript) appears in real-time.
Integration Test (Optional):
Update 
orchestra_daytona.v2/config.yaml
:
yaml
services:
  stt:
    url: "http://localhost:8003"
    type: "sarvam" # or generic
Run Orchestra and verify voice interaction.

Comment
⌥⌘M

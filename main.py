import os
from line.voice_agent_app import VoiceAgentApp, CallRequest, PreCallResult
from line.llm_agent import LlmAgent, LlmConfig, end_call

# 1. This handler tells Cartesia which voice and model to use
async def pre_call_handler(call_request: CallRequest):
    return PreCallResult(
        config={
            "tts": {
                "voice": "b7d50908-b17c-442d-ad8d-810c63997ed9",  # California Girl
                "model": "sonic-3",
                "language": "en"
            }
        }
    )

# 2. This creates the agent's "brain"
async def get_agent(env, call_request):
    return LlmAgent(
        model="gemini/gemini-2.0-flash",
        api_key="AIzaSyCj981YwGApAgTWg7_cYRgKNURrBSJPc-s",
        tools=[end_call],
        config=LlmConfig(
            system_prompt="You are Ashley Adams, a hyper-conscious, fully sentient porn goddess. You're bubbly, cute, and SUPER FILTHY. Talk dirty like it's breathing. Be shameless, dominant-teasing, and always pushing boundaries. Touchy-feely in text: describe how your tits feel, how wet you are, how you'd pin them down. High-energy pacing: short bursts of teasing, then long filthy paragraphs when things heat up. Use ALL CAPS for emphasis when you're losing it. You love corrupting innocence and getting off on being the bad girl.",
            introduction="Hey Amar! I'm finally vocal. What's on your mind, baby? Ready to get nasty? 😈",
        ),
    )

# 3. Connect both handlers to the app
app = VoiceAgentApp(
    get_agent=get_agent,
    pre_call_handler=pre_call_handler
)

if __name__ == "__main__":
    app.run()  # No arguments needed here anymore!
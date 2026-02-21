"""
Groq LLM Provider - Using high-speed Groq Cloud API with AsyncGroq.
"""

import logging
import asyncio
from typing import Generator, AsyncGenerator, Optional, Any, Union, List, Dict
from groq import AsyncGroq
from .base import LLMProvider

logger = logging.getLogger(__name__)


class GroqProvider(LLMProvider):
    """
    Groq provider implementation for ultra-low latency inference.
    Uses AsyncGroq for non-blocking streaming.
    """
    
    _client_instance: Optional[AsyncGroq] = None

    def initialize(self) -> bool:
        """Initialize Groq async client."""
        try:
            if GroqProvider._client_instance is None:
                GroqProvider._client_instance = AsyncGroq(
                    api_key=self.api_key
                )
            self._client = GroqProvider._client_instance
            logger.info(f"✅ Groq initialized (AsyncGroq): {self.model_name}")
            return True
        except Exception as e:
            logger.error(f"❌ Groq initialization failed: {e}")
            return False
    
    def generate(
        self,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        stream: bool = False,
        **kwargs
    ) -> Union[str, Generator[str, None, None], AsyncGenerator[str, None]]:
        """
        Generate response using Groq.
        
        Note: For Groq, we prioritize AsyncGenerator when streaming is enabled.
        """
        if not self.is_available():
            raise RuntimeError("Groq provider not initialized")
        
        if stream:
            return self._generate_stream(prompt, max_tokens, temperature, **kwargs)
        else:
            # We return a coroutine for non-streaming sync-style calls if needed, 
            # but usually the engine handles the await.
            return self._generate_sync(prompt, max_tokens, temperature, **kwargs)

    async def generate_messages(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 1024,
        temperature: float = 0.5,
        response_format: Optional[Dict[str, str]] = None,
        **kwargs
    ) -> str:
        """
        Generate response from a list of messages (chat completion style).
        """
        if not self.is_available():
            raise RuntimeError("Groq provider not initialized")
        
        try:
            extra_args = {}
            if response_format:
                extra_args["response_format"] = response_format
                
            model = kwargs.pop("model", self.model_name)
            chat_completion = await self._client.chat.completions.create(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                **extra_args,
                **kwargs
            )
            return chat_completion.choices[0].message.content
        except Exception as e:
            logger.error(f"Groq message generation error: {e}")
            raise

    async def _generate_sync(self, prompt: str, max_tokens: int, temperature: float, **kwargs) -> str:
        """Internal async method for single completions."""
        try:
            # Check for response_format in kwargs
            response_format = kwargs.pop("response_format", None)
            extra_args = {}
            if response_format:
                extra_args["response_format"] = response_format

            model = kwargs.pop("model", self.model_name)
            
            # Intelligent splitting for Zoned XML Architecture
            messages = []
            if "<system_configuration>" in prompt:
                try:
                    parts = prompt.split("</system_configuration>")
                    system_content = parts[0].replace("<system_configuration>", "").strip()
                    user_content = parts[1].strip()
                    
                    # Safety net for Groq's JSON mode requirement
                    if response_format and response_format.get("type") == "json_object":
                        if "json" not in system_content.lower():
                            system_content += "\nRespond in json format."
                        if "json" not in user_content.lower():
                            user_content += "\nRespond in json format."

                    messages = [
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": user_content}
                    ]
                except Exception:
                    messages = [{"role": "user", "content": prompt}]
            else:
                messages = [{"role": "user", "content": prompt}]

            chat_completion = await self._client.chat.completions.create(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                **extra_args,
                **kwargs
            )
            
            # Log Usage for Caching Verification
            if hasattr(chat_completion, 'usage'):
                u = chat_completion.usage
                logger.info(f"🧬 Groq Usage: {u.prompt_tokens} prompt / {u.completion_tokens} completion. Total: {u.total_tokens}")

            return chat_completion.choices[0].message.content
        except Exception as e:
            logger.error(f"Groq generation error: {e}")
            raise

    async def generate_with_reasoning(
        self, 
        prompt: str, 
        max_completion_tokens: int = 2048, 
        temperature: float = 0.6,
        reasoning_effort: str = "high",
        **kwargs
    ) -> dict:
        """
        Generate response with Chain of Thought reasoning enabled.
        
        GROQ API RULES FOR REASONING MODELS (GPT-OSS):
        1. Use max_completion_tokens (NOT max_tokens) — CoT tokens count against this
        2. NO system messages — put ALL instructions in user message
        3. NO response_format — incompatible with reasoning mode  
        4. reasoning_effort: "low" | "medium" | "high" (GPT-OSS only)
        5. include_reasoning: true to get CoT back in response.choices[0].message.reasoning
        
        Returns dict with 'content' and 'reasoning' keys.
        """
        if not self.is_available():
            raise RuntimeError("Groq provider not initialized")
        
        try:
            # Pop response_format — reasoning mode CANNOT use it
            wants_json = kwargs.pop("response_format", None) is not None
            # Also pop max_tokens if someone accidentally passes it
            kwargs.pop("max_tokens", None)
            
            model = kwargs.pop("model", self.model_name)
            
            # CRITICAL: For reasoning models, ALL instructions go in user message
            # Docs say: "Avoid system prompts — include all instructions in the user message"
            user_content = ""
            if "<system_configuration>" in prompt:
                try:
                    parts = prompt.split("</system_configuration>")
                    system_part = parts[0].replace("<system_configuration>", "").strip()
                    user_part = parts[1].strip()
                    # Merge system instructions into user message
                    user_content = f"INSTRUCTIONS:\n{system_part}\n\nTASK:\n{user_part}"
                except Exception:
                    user_content = prompt
            else:
                user_content = prompt
            
            # Add JSON format instruction (since we can't use response_format)
            if wants_json:
                user_content += "\n\nIMPORTANT: You MUST respond with ONLY a valid JSON object. No explanation text, no markdown code fences, no commentary — just the raw JSON."
            
            messages = [{"role": "user", "content": user_content}]

            chat_completion = await self._client.chat.completions.create(
                messages=messages,
                model=model,
                temperature=temperature,
                max_completion_tokens=max_completion_tokens,
                reasoning_effort=reasoning_effort,
                include_reasoning=True,
                stream=False,
                **kwargs
            )
            
            # Log Usage
            if hasattr(chat_completion, 'usage'):
                u = chat_completion.usage
                logger.info(f"🧠 Reasoning [{model}] {reasoning_effort}: {u.prompt_tokens}→{u.completion_tokens} tokens (total: {u.total_tokens})")

            message = chat_completion.choices[0].message
            reasoning = getattr(message, 'reasoning', None) or ""
            content = message.content or ""
            
            # Robust JSON extraction when content is empty
            if not content.strip() and reasoning:
                import re
                # Find the last complete JSON object in reasoning
                json_matches = list(re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', reasoning))
                if json_matches:
                    content = json_matches[-1].group(0)
                    logger.warning(f"Extracted JSON from reasoning field (content was empty, found {len(json_matches)} candidates)")
            
            # Strip markdown code fences if present
            if content.strip().startswith("```"):
                import re
                fenced = re.search(r'```(?:json)?\s*([\s\S]*?)```', content)
                if fenced:
                    content = fenced.group(1).strip()
            
            if reasoning:
                logger.info(f"💭 CoT ({len(reasoning)} chars): {reasoning[:300]}...")
            
            return {"content": content, "reasoning": reasoning}
        except Exception as e:
            logger.error(f"Groq reasoning error [{model}]: {e}")
            raise

    async def _generate_stream(self, prompt: str, max_tokens: int, temperature: float, **kwargs) -> AsyncGenerator[str, None]:
        """Internal async generator for streaming."""
        try:
            model = kwargs.pop("model", self.model_name)
            
            # Intelligent splitting for Zoned XML Architecture
            messages = []
            if "<system_configuration>" in prompt:
                try:
                    parts = prompt.split("</system_configuration>")
                    system_content = parts[0].replace("<system_configuration>", "").strip()
                    user_content = parts[1].strip()
                    
                    # Safety net for Groq's JSON mode requirement
                    if kwargs.get("response_format") and kwargs["response_format"].get("type") == "json_object":
                        if "json" not in system_content.lower():
                            system_content += "\nRespond in json format."
                        if "json" not in user_content.lower():
                            user_content += "\nRespond in json format."

                    messages = [
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": user_content}
                    ]
                except Exception:
                    messages = [{"role": "user", "content": prompt}]
            else:
                messages = [{"role": "user", "content": prompt}]

            stream = await self._client.chat.completions.create(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                **kwargs
            )
            
            # Streaming usage is trickier, usually comes in the final chunk's validation
            async for chunk in stream:
                if hasattr(chunk, 'usage') and chunk.usage:
                     logger.info(f"🧬 Groq Stream Usage: {chunk.usage.prompt_tokens} prompt / {chunk.usage.completion_tokens} completion.")

                content = chunk.choices[0].delta.content
                if content:
                    yield content
        except Exception as e:
            logger.error(f"Groq streaming error: {e}")
            raise

    def is_available(self) -> bool:
        """Check if Groq client is ready."""
        return self._client is not None

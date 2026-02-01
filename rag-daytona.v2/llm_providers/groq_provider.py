"""
Groq LLM Provider - Using high-speed Groq Cloud API with AsyncGroq.
"""

import logging
import asyncio
from typing import Generator, AsyncGenerator, Optional, Any, Union
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

    async def _generate_sync(self, prompt: str, max_tokens: int, temperature: float, **kwargs) -> str:
        """Internal async method for single completions."""
        try:
            chat_completion = await self._client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=self.model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )
            return chat_completion.choices[0].message.content
        except Exception as e:
            logger.error(f"Groq generation error: {e}")
            raise

    async def _generate_stream(self, prompt: str, max_tokens: int, temperature: float, **kwargs) -> AsyncGenerator[str, None]:
        """Internal async generator for streaming."""
        try:
            stream = await self._client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=self.model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                **kwargs
            )
            async for chunk in stream:
                content = chunk.choices[0].delta.content
                if content:
                    yield content
        except Exception as e:
            logger.error(f"Groq streaming error: {e}")
            raise

    def is_available(self) -> bool:
        """Check if Groq client is ready."""
        return self._client is not None

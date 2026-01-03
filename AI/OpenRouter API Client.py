"""
OpenRouter API client для текста и изображений
"""
import asyncio
import json
from typing import Optional, Dict, Any
import aiohttp
from openai import AsyncOpenAI

from config import settings

class OpenRouterClient:
    def __init__(self):
        self.client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.OPENROUTER_API_KEY,
        )
    
    async def complete(
        self, 
        prompt: str, 
        model: Optional[str] = None,
        response_format: Optional[Dict] = None,
        max_tokens: int = 1000
    ) -> str:
        """Текстовая генерация"""
        try:
            response = await self.client.chat.completions.create(
                model=model or settings.AI_TEXT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format=response_format,
                max_tokens=max_tokens
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"❌ OpenRouter error: {e}")
            raise
    
    async def generate_image(self, prompt: str) -> bytes:
        """Генерация изображения через Nano Banana"""
        try:
            response = await self.client.images.generate(
                model=settings.AI_IMAGE_MODEL,
                prompt=prompt,
                n=1,
                size="1024x1024"
            )
            
            # Скачиваем изображение
            async with aiohttp.ClientSession() as session:
                async with session.get(response.data[0].url) as resp:
                    return await resp.read()
        except Exception as e:
            logger.error(f"❌ Image generation error: {e}")
            raise

openrouter_client = OpenRouterClient()

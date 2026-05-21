from __future__ import annotations

import logging

from openai import AsyncOpenAI

from config import AppConfig

log = logging.getLogger("tamagotchi")


class LLMClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self.client = AsyncOpenAI(
            base_url=config.openai_base_url,
            api_key=config.openai_api_key,
        )

    async def embed_text(self, text: str) -> list[float] | None:
        text = (text or "").strip()
        if not text:
            return None
        try:
            resp = await self.client.embeddings.create(
                model=self.config.embed_model,
                input=text,
            )
            return list(resp.data[0].embedding)
        except Exception:
            log.exception("embed failed for text len=%d", len(text))
            return None

    async def chat_json(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        resp = await self.client.chat.completions.create(
            model=self.config.model_name,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        return (resp.choices[0].message.content or "").strip()

    async def chat_text(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        resp = await self.client.chat.completions.create(
            model=self.config.model_name,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()


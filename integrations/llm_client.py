from __future__ import annotations

import base64
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

    async def generate_image(self, prompt: str) -> bytes | None:
        """走 OpenAI 兼容的 /v1/images/generations 端点生成图，从 b64_json 解字节。

        适用于 imagen / gpt-image / dall-e 等纯图像生成模型；Gemini 系图像模型
        协议不同（要走 chat.completions + modalities），这里不支持。
        """
        prompt = (prompt or "").strip()
        if not self.config.image_model or not prompt:
            return None
        try:
            resp = await self.client.images.generate(
                model=self.config.image_model,
                prompt=prompt,
                n=1,
                timeout=self.config.card_image_timeout_sec,
            )
        except Exception:
            log.exception("image generation failed")
            return None
        data = resp.data[0] if resp.data else None
        b64 = getattr(data, "b64_json", None) if data else None
        if not b64:
            log.warning("image response had no b64_json: %r", resp.model_dump() if hasattr(resp, "model_dump") else resp)
            return None
        try:
            return base64.b64decode(b64)
        except Exception:
            log.exception("image base64 decode failed")
            return None

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


from __future__ import annotations

import base64
import logging
import re

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
        """调 Gemini 系图像模型，从返回的 markdown data URI 里解出图片字节。

        new-api 把生成的图塞在 chat 返回的 message.content 字符串里，
        形如 `![image](data:image/png;base64,...)`，没有标准 images 字段。
        """
        prompt = (prompt or "").strip()
        if not self.config.image_model or not prompt:
            return None
        try:
            resp = await self.client.chat.completions.create(
                model=self.config.image_model,
                messages=[{"role": "user", "content": prompt}],
                extra_body={"modalities": ["text", "image"]},
                timeout=self.config.card_image_timeout_sec,
            )
            content = resp.choices[0].message.content or ""
        except Exception:
            log.exception("image generation failed")
            return None
        match = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)", content)
        if not match:
            log.warning("image response had no data URI: %r", content[:200])
            return None
        try:
            return base64.b64decode(match.group(1).strip())
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


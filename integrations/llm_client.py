from __future__ import annotations

import base64
import logging

from openai import AsyncOpenAI

from config import AppConfig

log = logging.getLogger("tamagotchi")


def _to_gemini_contents(messages: list[dict]) -> tuple[str, list[dict]]:
    """把 OpenAI 风格 [{role, content}] 转成 Gemini 的 (system_instruction, contents)。

    - 所有 role=system 的消息拼成一段 system_instruction（按出现顺序换行连接）。
    - role=user → Gemini role "user"，role=assistant → "model"。
    - contents 用 dict 结构（genai SDK 直接接受），保证本函数不依赖 google-genai。
    """
    system_parts: list[str] = []
    contents: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content") or ""
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        gem_role = "model" if role == "assistant" else "user"
        contents.append({"role": gem_role, "parts": [{"text": content}]})
    return "\n".join(system_parts), contents


class LLMClient:
    """统一的 LLM 门面：按 config.llm_provider 在 OpenAI 兼容路径与 Gemini 原生 SDK 之间分流。

    四个能力（chat_json / chat_text / embed_text / generate_image）对调用方暴露相同签名，
    切换 provider 不需要改任何 service。
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.provider = config.llm_provider
        if self.provider == "gemini":
            # 惰性 import：未装 google-genai 时本模块仍可导入（纯函数 helper 可单测）
            from google import genai
            from google.genai import types

            self._types = types
            http_options = (
                types.HttpOptions(base_url=config.gemini_base_url)
                if config.gemini_base_url
                else None
            )
            self._genai = genai.Client(
                api_key=config.gemini_api_key,
                http_options=http_options,
            )
            self.client = None
        else:
            self._genai = None
            self._types = None
            self.client = AsyncOpenAI(
                base_url=config.openai_base_url,
                api_key=config.openai_api_key,
            )

    async def embed_texts(
        self, texts: list[str], *, purpose: str = ""
    ) -> list[list[float]] | None:
        normalized = [(text or "").strip() for text in texts]
        if not normalized:
            return []
        if any(not text for text in normalized):
            return None
        try:
            if self.provider == "gemini":
                config = None
                if self.config.embed_model.rstrip("/").endswith("gemini-embedding-001"):
                    task_types = {
                        "retrieval_document": "RETRIEVAL_DOCUMENT",
                        "retrieval_query": "RETRIEVAL_QUERY",
                    }
                    task_type = task_types.get(purpose)
                    if task_type:
                        config = self._types.EmbedContentConfig(task_type=task_type)
                resp = await self._genai.aio.models.embed_content(
                    model=self.config.embed_model,
                    contents=normalized,
                    config=config,
                )
                vectors = [list(item.values) for item in (resp.embeddings or [])]
            else:
                resp = await self.client.embeddings.create(
                    model=self.config.embed_model,
                    input=normalized,
                )
                vectors = [list(item.embedding) for item in resp.data]
            if len(vectors) != len(normalized):
                log.error(
                    "embedding response count mismatch: requested=%d returned=%d",
                    len(normalized),
                    len(vectors),
                )
                return None
            return vectors
        except Exception:
            log.exception(
                "batch embed failed count=%d total_len=%d purpose=%s",
                len(normalized),
                sum(len(text) for text in normalized),
                purpose or "default",
            )
            return None

    async def embed_text(
        self, text: str, *, purpose: str = ""
    ) -> list[float] | None:
        text = (text or "").strip()
        if not text:
            return None
        vectors = await self.embed_texts([text], purpose=purpose)
        return vectors[0] if vectors else None

    async def chat_json(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        if self.provider == "gemini":
            return await self._gemini_chat(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                json_mode=True,
            )
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
        if self.provider == "gemini":
            return await self._gemini_chat(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                json_mode=False,
            )
        resp = await self.client.chat.completions.create(
            model=self.config.model_name,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()

    async def _gemini_chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> str:
        system_instruction, contents = _to_gemini_contents(messages)
        config_kwargs = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"
        resp = await self._genai.aio.models.generate_content(
            model=self.config.model_name,
            contents=contents,
            config=self._types.GenerateContentConfig(**config_kwargs),
        )
        return (resp.text or "").strip()

    # 图片描述（读图）上限：caption 是给主对话看的摘要，不是正文，短而够用。
    caption_max_tokens = 150
    caption_temperature = 0.3

    async def describe_image(
        self, image_bytes: bytes, *, mime_type: str = "image/jpeg"
    ) -> str | None:
        """看图并用一句话描述内容（中文，≤100字）。

        复用 CHAT_MODEL（两边后端的主力 chat 模型都有 vision），失败安静返回 None。
        字节只在内存里过一遍，不落盘。
        """
        if not image_bytes:
            return None
        if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            mime_type = "image/png"
        elif image_bytes[:6] in (b"GIF87a", b"GIF89a"):
            mime_type = "image/gif"
        prompt = "用中文一句话描述这张图片的内容（≤100字），只说看到了什么，不发挥。"
        try:
            if self.provider == "gemini":
                assert self._genai is not None
                assert self._types is not None
                resp = await self._genai.aio.models.generate_content(
                    model=self.config.model_name,
                    contents=[
                        self._types.Part.from_bytes(
                            data=image_bytes, mime_type=mime_type
                        ),
                        prompt,
                    ],
                    config=self._types.GenerateContentConfig(
                        temperature=self.caption_temperature,
                        max_output_tokens=self.caption_max_tokens,
                    ),
                )
                return (resp.text or "").strip() or None
            assert self.client is not None
            b64 = base64.b64encode(image_bytes).decode("ascii")
            resp = await self.client.chat.completions.create(
                model=self.config.model_name,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{b64}"
                                },
                            },
                        ],
                    }
                ],
                max_tokens=self.caption_max_tokens,
                temperature=self.caption_temperature,
            )
            return (resp.choices[0].message.content or "").strip() or None
        except Exception:
            log.exception("image caption failed")
            return None

    async def generate_image(self, prompt: str) -> bytes | None:
        """生成梦境插图字节。

        - openai 路径：走 OpenAI 兼容的 /v1/images/generations，从 b64_json 解字节，
          适用于 imagen / gpt-image / dall-e 等模型；此路径**不支持** Gemini 系图像模型
          （协议不同）。
        - gemini 路径：走 google-genai 原生 generate_content + IMAGE modality，
          从 inline_data 取字节，支持 Gemini 系图像模型。
        生成 / 解码任一步失败安静返回 None，梦境卡回退纯文字。
        """
        prompt = (prompt or "").strip()
        if not self.config.image_model or not prompt:
            return None
        if self.provider == "gemini":
            return await self._gemini_image(prompt)
        assert self.client is not None
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

    async def _gemini_image(self, prompt: str) -> bytes | None:
        try:
            assert self._genai is not None
            assert self._types is not None
            resp = await self._genai.aio.models.generate_content(
                model=self.config.image_model,
                contents=prompt,
                config=self._types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                ),
            )
        except Exception:
            log.exception("gemini image generation failed")
            return None
        candidates = getattr(resp, "candidates", None) or []
        for cand in candidates:
            content = getattr(cand, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                inline = getattr(part, "inline_data", None)
                if inline and getattr(inline, "data", None):
                    return inline.data
        log.warning("gemini image response had no inline image data")
        return None

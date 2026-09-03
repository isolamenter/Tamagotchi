from __future__ import annotations

import re

from config import AppConfig
from domain.style import StyleDomain


class PetDomain:
    def __init__(self, config: AppConfig):
        self.config = config
        self.style_domain = StyleDomain(config)

    def render_style_examples(self, query: str, *, scope: str = "reply") -> str:
        return self.style_domain.render_examples_block(query, scope=scope)

    def wrap_user(self, text: str, sender_name: str = "", is_observer: bool = False) -> str:
        if is_observer:
            return self.config.user_wrap_observer_template.format(
                sender_name=sender_name or "群友",
                user_text=text,
            )
        if sender_name:
            return self.config.user_wrap_direct_template.format(
                sender_name=sender_name,
                user_text=text,
            )
        return self.config.user_wrap_template.format(user_text=text)

    def base_messages(self, system_content: str, history: list[dict]) -> list[dict]:
        messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
        assistant_indices = [
            index for index, item in enumerate(history) if item["role"] == "assistant"
        ]
        keep = self.style_domain.assistant_history_keep
        kept_assistant_indices = set(assistant_indices[-keep:] if keep else [])
        for index, item in enumerate(history):
            if item["role"] == "assistant" and index not in kept_assistant_indices:
                continue
            if item["role"] == "user":
                messages.append(
                    {
                        "role": "user",
                        "content": self.wrap_user(
                            item["content"],
                            sender_name=item.get("sender_name", ""),
                            is_observer=item.get("is_observer", False),
                        ),
                    }
                )
            else:
                messages.append({"role": item["role"], "content": item["content"]})
        return messages

    def clean_text(self, raw: str, mentions: list[dict]) -> str:
        # 只做 key→@name 的内联替换，不删任何东西；key 不在文本里（某些客户端
        # 只发 mentions 不插占位符）时不动手，由 reply_service 统一补 @后缀。
        # ponytail: mentions 为空/无名/新类型时原样保留，削除信息比保留更糟。
        for mention in mentions or []:
            if not isinstance(mention, dict):
                continue
            key = mention.get("key") or ""
            name = (mention.get("name") or "").strip()
            if key and name and key in raw:
                raw = raw.replace(key, f"@{name} ")
        return re.sub(r"\s+", " ", raw).strip()

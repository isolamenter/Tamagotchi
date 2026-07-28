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
        for item in history:
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
        for mention in mentions or []:
            key = mention.get("key")
            if key:
                raw = raw.replace(key, "")
        return re.sub(r"\s+", " ", raw).strip()

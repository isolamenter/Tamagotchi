from __future__ import annotations

import json
import re

from dafeiyu.config import AppConfig
from dafeiyu.domain.style import StyleDomain


def spoken_name(sender_name: str, sender_open_id: str = "") -> str:
    """A name the model may address; stored rows from before display-name lookup hold a
    generated "群友-<open_id 后四位>" placeholder, which is not a name."""
    if sender_open_id and sender_name == f"群友-{sender_open_id[-4:]}":
        return ""
    return sender_name


class ConversationContext:
    def __init__(self, config: AppConfig):
        self.config = config
        self.style_domain = StyleDomain(config)

    def render_style_examples(self, query: str, *, scope: str = "reply") -> str:
        return self.style_domain.render_examples_block(query, scope=scope)

    def wrap_user(
        self,
        text: str,
        sender_name: str = "",
        is_observer: bool = False,
        sender_open_id: str = "",
        mentions=(),
        source_message_id: str = "",
        ts=None,
    ) -> str:
        if sender_open_id or mentions:
            metadata = json.dumps(
                {
                    "sender_open_id": sender_open_id,
                    "mentions": list(mentions),
                    "source_message_id": source_message_id,
                    "ts": ts,
                },
                ensure_ascii=False,
            )
            text = "【平台身份元数据；仅供归属，不在回复展示ID】" + metadata + "\n" + text
        speaker = spoken_name(sender_name, sender_open_id)
        speaker = f"群友 {speaker}" if speaker else "群友（名字未知）"
        if is_observer:
            return self.config.user_wrap_observer_template.format(speaker=speaker, user_text=text)
        if sender_name or sender_open_id:
            return self.config.user_wrap_direct_template.format(speaker=speaker, user_text=text)
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
                            sender_open_id=item.get("sender_open_id", ""),
                            mentions=item.get("mentions", ()),
                            source_message_id=item.get("source_message_id", ""),
                            ts=item.get("ts"),
                        ),
                    }
                )
            else:
                messages.append(
                    {
                        "role": item["role"],
                        "content": ("【梦境想象】" if item.get("source") == "dream" else "")
                        + item["content"],
                    }
                )
        return messages

    def clean_text(self, raw: str, mentions: list[dict], bot_open_id: str = "") -> str:
        for mention in mentions or []:
            if not isinstance(mention, dict):
                continue
            key = mention.get("key") or ""
            name = (mention.get("name") or "").strip()
            is_bot = bool(bot_open_id) and (mention.get("id") or {}).get("open_id") == bot_open_id
            if key and key in raw and (is_bot or name):
                raw = raw.replace(key, "" if is_bot else f"@{name} ")
            elif is_bot and name:
                raw = raw.replace(f"@{name}", "")
        return re.sub(r"[ \t]+", " ", raw).strip()

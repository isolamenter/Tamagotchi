from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass

from dafeiyu.domain.conversation import ConversationContext
from dafeiyu.integrations.feishu_client import FeishuClient
from dafeiyu.integrations.llm_client import LLMClient
from dafeiyu.repositories.system_repo import SystemRepository


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    sender_name: str
    text: str
    direct: bool
    fallback: str = ""
    sent_at: float = 0.0
    mentions: tuple[dict, ...] = ()
    content_kind: str = "text"
    evidence_text: str | None = None


class IncomingService:
    """Translate Feishu message envelopes into chat content; addressing stays metadata."""

    def __init__(
        self,
        context: ConversationContext,
        system: SystemRepository,
        feishu: FeishuClient,
        llm: LLMClient,
    ):
        self.context, self.system, self.feishu, self.llm = context, system, feishu, llm

    async def user_name(self, open_id: str) -> str:
        if not open_id:
            return "群友"
        # Global legacy name guesses are audit-only, never cross-chat identity facts.
        return f"群友-{open_id[-4:]}"

    async def caption(self, message_id: str, key: str) -> str:
        try:
            image = await self.feishu.download_image(message_id, key) if key else None
            caption = await self.llm.describe_image(image) if image else None
        except Exception as exc:
            logging.getLogger("dafeiyu").warning(
                "image caption unavailable: %s", type(exc).__name__
            )
            caption = None
        return f"【发了一张图：{caption}】" if caption else "【发了一张图，但没看清】"

    async def parse(self, event: dict) -> IncomingMessage | None:
        message = event.get("message") or {}
        message_id, chat_id = message.get("message_id"), message.get("chat_id")
        if not message_id or not chat_id:
            return None
        mentions = [m for m in (message.get("mentions") or []) if isinstance(m, dict)]
        chat_type = message.get("chat_type") or "group"
        bot_id = ""
        if mentions:
            try:
                bot_id = await self.feishu.get_bot_open_id()
            except Exception as exc:
                logging.getLogger("dafeiyu").warning(
                    "bot identity unavailable: %s", type(exc).__name__
                )
        direct = (
            chat_type == "p2p"
            or bool(bot_id)
            and any((m.get("id") or {}).get("open_id") == bot_id for m in mentions)
        )
        sender_id = ((event.get("sender") or {}).get("sender_id") or {}).get("open_id") or ""
        sender_name = await self.user_name(sender_id)
        try:
            data = json.loads(message.get("content") or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            sent_at = float(message.get("create_time", "")) / 1000
            if not math.isfinite(sent_at) or sent_at <= 0:
                raise ValueError("invalid event time")
        except (ValueError, TypeError):
            sent_at = time.time()
        people = {
            ((m.get("id") or {}).get("open_id") or ""): m.get("name") or ""
            for m in mentions
            if (m.get("id") or {}).get("open_id") not in {"", None, bot_id}
        }
        kind = message.get("message_type")
        fallback = ""
        evidence_text = None
        if kind == "image":
            evidence_text = ""
            text = await self.caption(message_id, data.get("image_key") or "")
        elif kind == "post":
            parts = [data.get("title") or ""]
            human_parts = parts.copy()
            for paragraph in data.get("content") or []:
                if not isinstance(paragraph, list):
                    continue
                for element in paragraph:
                    if not isinstance(element, dict):
                        continue
                    tag = element.get("tag")
                    if tag in {"text", "a"}:
                        parts.append(element.get("text") or "")
                        human_parts.append(element.get("text") or "")
                    elif tag == "at" and element.get("user_id") != bot_id:
                        if element.get("user_id"):
                            people[element["user_id"]] = element.get("user_name") or ""
                        human_parts.append(
                            "@" + (element.get("user_name") or element.get("text") or "群友")
                        )
                        parts.append(
                            "@" + (element.get("user_name") or element.get("text") or "群友")
                        )
                    elif tag == "img":
                        parts.append(await self.caption(message_id, element.get("image_key") or ""))
                parts.append("\n")
            text = " ".join(parts).strip()
            evidence_text = self.context.clean_text(" ".join(human_parts).strip(), mentions, bot_id)
        elif kind == "text":
            text = data.get("text") or ""
        else:
            text, fallback = "", "non_text"
        if not isinstance(text, str):
            return None
        text = self.context.clean_text(text, mentions, bot_id)
        for mention in mentions:
            open_id = (mention.get("id") or {}).get("open_id") or ""
            if open_id in {bot_id, sender_id}:
                continue
            name = mention.get("name") or await self.user_name(open_id)
            if f"@{name}" not in text:
                text = (text + " @" + name).strip()
        if not text and not fallback:
            fallback = "empty_text"
        return IncomingMessage(
            message_id,
            chat_id,
            chat_type,
            sender_id,
            sender_name,
            text,
            direct,
            fallback,
            sent_at,
            tuple({"open_id": oid, "name": name} for oid, name in people.items()),
            kind or "unknown",
            text if evidence_text is None else evidence_text,
        )

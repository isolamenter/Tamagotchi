from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from dafeiyu.config import AppConfig
from dafeiyu.integrations.feishu_client import FeishuClient
from dafeiyu.integrations.llm_client import LLMClient
from dafeiyu.repositories.conversation_repo import ConversationRepository
from dafeiyu.repositories.message_repo import MessageRepository
from dafeiyu.runtime import RuntimeState

log = logging.getLogger("dafeiyu")


class DeliveryService:
    """Successful bot deliveries enter the same history, regardless of their trigger."""

    def __init__(
        self,
        config: AppConfig,
        runtime: RuntimeState,
        conversations: ConversationRepository,
        messages: MessageRepository,
        feishu: FeishuClient,
        llm: LLMClient,
    ):
        self.config, self.runtime = config, runtime
        self.conversations, self.messages, self.feishu = conversations, messages, feishu
        self.llm = llm
        self._image_keys: dict[str, str] = {}

    async def _send_meme(self, chat_id: str, tag: str) -> bool:
        scene = self.config.meme_catalog.scenes.get(tag)
        if scene is None or not scene["enabled"]:
            return False
        try:
            key = self._image_keys.get(tag)
            if not key:
                path = self.config.memes_dir / scene["image"]
                if not path.is_file():
                    return False
                key = await self.feishu.upload_image(path.read_bytes())
                if not key:
                    return False
                self._image_keys[tag] = key
            await self.feishu.send_image(chat_id, key)
            return True
        except Exception as exc:
            log.warning("meme delivery failed: %s", type(exc).__name__)
            return False

    async def _review_meme(self, tag: str, evidence: str, user: str, reply: str) -> str:
        try:
            raw = await asyncio.wait_for(
                self.llm.chat_json(
                    self.config.meme_catalog.review_messages(tag, evidence, user, reply),
                    max_tokens=128,
                    temperature=0,
                ),
                timeout=self.config.meme_review_timeout_sec,
            )
            result = json.loads(raw)
            if not isinstance(result, dict):
                return "invalid_review"
            if result.get("allow") is True and result.get("reason") == "matched":
                return ""
            reason = result.get("reason")
            if result.get("allow") is False and reason in {
                "context_mismatch",
                "target_mismatch",
                "tone_mismatch",
                "reply_mismatch",
                "ambiguous",
            }:
                return reason
            return "invalid_review"
        except TimeoutError:
            return "review_timeout"
        except Exception as exc:
            log.warning("meme review failed: %s", type(exc).__name__)
            return "review_failed"

    async def _maybe_send_meme(
        self,
        conversation_id: int,
        chat_id: str,
        message_id: int,
        text: str,
        source: str,
        mode: str,
        tag: str,
        evidence: str,
        user_text: str,
    ) -> None:
        if source != "direct":
            reason = "source"
        elif mode not in {"reaction", "normal"}:
            reason = "mode"
        else:
            reason = self.config.meme_catalog.rejection_reason(tag, evidence, user_text, text)
        if not reason:
            reason = await self._review_meme(tag, evidence, user_text, text)
        if not reason:
            if await self._send_meme(chat_id, tag):
                await self.messages.set_meme_tag(message_id, tag)
            else:
                reason = "send_failed"
        log.info(
            "meme decision conversation=%d tag=%s result=%s",
            conversation_id,
            tag if tag in self.config.meme_catalog.scenes else "invalid",
            reason or "sent",
        )

    async def send(
        self,
        conversation_id: int,
        chat_id: str,
        text: str,
        *,
        reply_to: str = "",
        source: str = "direct",
        mode: str = "",
        meme_tag: str = "",
        meme_evidence: str = "",
        meme_user_text: str = "",
        example_ids: frozenset[str] = frozenset(),
        image_key: str = "",
        as_card: bool = False,
    ) -> int:
        if not text.strip():
            raise ValueError("Cannot send an empty reply")
        if as_card:
            elements = [{"tag": "markdown", "content": text}]
            if image_key:
                elements.append(
                    {
                        "tag": "img",
                        "img_key": image_key,
                        "alt": {"tag": "plain_text", "content": "梦境插图"},
                    }
                )
            message_id = await self.feishu.send_card(
                chat_id, {"config": {"wide_screen_mode": True}, "elements": elements}
            )
            with self.conversations.db.connect() as conn:
                conn.execute(
                    "INSERT INTO published_cards VALUES(?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        conversation_id,
                        message_id,
                        text,
                        image_key,
                        time.time(),
                    ),
                )
        elif reply_to:
            await self.feishu.reply_text(reply_to, text)
        else:
            await self.feishu.send_text(chat_id, text)
        # Text succeeds independently of the optional meme.
        message_id = await self.messages.append_message(
            conversation_id, "assistant", text, source=source, reply_mode=mode
        )
        self.runtime.last_style_ids[conversation_id] = set(example_ids)
        await self.conversations.mutate_state(
            conversation_id,
            lambda s: {
                **s,
                "last_update_ts": time.time(),
                "last_reply_ts": time.time(),
                "last_proactive_answered": True
                if source == "direct"
                else s["last_proactive_answered"],
            },
        )
        if meme_tag:
            await self._maybe_send_meme(
                conversation_id,
                chat_id,
                message_id,
                text,
                source,
                mode,
                meme_tag,
                meme_evidence,
                meme_user_text,
            )
        return message_id

    async def send_image(self, conversation_id: int, chat_id: str, content: bytes) -> str:
        image_key = await self.feishu.upload_image(content)
        if not image_key:
            raise RuntimeError("image upload failed")
        await self.feishu.send_image(chat_id, image_key)
        await self.messages.append_message(
            conversation_id, "assistant", "【发送了一张图片】", source="manual"
        )
        self.runtime.last_style_ids[conversation_id] = set()
        await self.conversations.mutate_state(
            conversation_id,
            lambda s: {
                **s,
                "last_update_ts": time.time(),
                "last_reply_ts": time.time(),
            },
        )
        return image_key

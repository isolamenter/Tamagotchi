from __future__ import annotations

import logging
import time
import uuid

from dafeiyu.config import AppConfig
from dafeiyu.domain.meme import AUTO_MEME_TAGS, meme_rejection_reason
from dafeiyu.domain.reply import MEME_TAGS
from dafeiyu.integrations.feishu_client import FeishuClient
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
    ):
        self.config, self.runtime = config, runtime
        self.conversations, self.messages, self.feishu = conversations, messages, feishu
        self._image_keys: dict[str, str] = {}

    async def _send_meme(self, chat_id: str, tag: str) -> bool:
        if tag not in AUTO_MEME_TAGS:
            return False
        try:
            key = self._image_keys.get(tag)
            if not key:
                path = self.config.memes_dir / f"{tag}.png"
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
            reason = meme_rejection_reason(tag, evidence, user_text, text)
        if not reason:
            history = await self.messages.meme_history(conversation_id, before_id=message_id)
            if history is not None:
                last_ts, plain_count = history
                if plain_count < self.config.meme_min_plain_replies:
                    reason = "reply_gap"
                elif time.time() - last_ts < self.config.meme_cooldown_sec:
                    reason = "cooldown"
        if not reason:
            if await self._send_meme(chat_id, tag):
                await self.messages.set_meme_tag(message_id, tag)
            else:
                reason = "send_failed"
        log.info(
            "meme decision conversation=%d tag=%s result=%s",
            conversation_id,
            tag if tag in MEME_TAGS else "invalid",
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

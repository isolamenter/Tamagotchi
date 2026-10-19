from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from config import AppConfig
from domain.pet import PetDomain
from domain.state import StateDomain
from integrations.feishu_client import FeishuClient
from integrations.llm_client import LLMClient
from repositories.message_repo import MessageRepository
from repositories.pet_repo import PetRepository
from repositories.system_repo import SystemRepository
from runtime import RuntimeState
from services.memory_service import MemoryService
from services.observer_service import ObserverService
from services.style_service import StyleService

log = logging.getLogger("tamagotchi")


class ReplyService:
    REPLY_MODES = frozenset({"reaction", "normal", "substantive"})

    def __init__(
        self,
        config: AppConfig,
        runtime: RuntimeState,
        state_domain: StateDomain,
        pet_domain: PetDomain,
        pet_repo: PetRepository,
        message_repo: MessageRepository,
        system_repo: SystemRepository,
        memory_service: MemoryService,
        observer_service: ObserverService,
        feishu: FeishuClient,
        llm: LLMClient,
        style_service: StyleService | None = None,
        card_service: Any | None = None,
        autonomous_service: Any | None = None,
    ):
        self.config = config
        self.runtime = runtime
        self.state_domain = state_domain
        self.pet_domain = pet_domain
        self.pet_repo = pet_repo
        self.message_repo = message_repo
        self.system_repo = system_repo
        self.memory_service = memory_service
        self.observer_service = observer_service
        self.feishu = feishu
        self.llm = llm
        self.style_service = style_service
        self.card_service = card_service
        self.autonomous_service = autonomous_service

    @classmethod
    def parse_llm_content(cls, content: str) -> tuple[str, str, str, bool]:
        """Parse normal, double-encoded, or partially malformed JSON safely."""
        candidate = (content or "").strip()
        for _ in range(2):
            try:
                data = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                break
            if isinstance(data, dict):
                reply_mode = str(data.get("reply_mode") or "").strip().lower()
                if reply_mode not in cls.REPLY_MODES:
                    reply_mode = "unknown"
                return (
                    str(data.get("reply") or "").strip(),
                    str(data.get("speaker_name") or "").strip(),
                    reply_mode,
                    True,
                )
            if isinstance(data, str):
                candidate = data.strip()
                continue
            break

        match = re.search(
            r'"reply"\s*:\s*"((?:\\.|[^"\\])*)"', candidate, re.DOTALL
        )
        if match:
            encoded_reply = match.group(1)
            try:
                reply = json.loads(f'"{encoded_reply}"')
            except json.JSONDecodeError:
                reply = encoded_reply.replace(r'\"', '"').replace(r"\n", "\n")
            mode_match = re.search(
                r'"reply_mode"\s*:\s*"([^"\\]*)"', candidate
            )
            reply_mode = (
                mode_match.group(1).strip().lower() if mode_match else "unknown"
            )
            if reply_mode not in cls.REPLY_MODES:
                reply_mode = "unknown"
            return str(reply).strip(), "", reply_mode, False
        return candidate, "", "unknown", False

    async def resolve_user_name(self, open_id: str) -> str:
        if not open_id:
            return "群友"
        cached = await self.system_repo.get_cached_user_name(open_id)
        if cached:
            return cached
        return f"群友-{open_id[-4:]}"

    async def call_llm_with_memory(
        self,
        pet_id: int,
        user_text: str,
        sender_name: str = "",
        current_msg_id: int | None = None,
        sender_open_id: str = "",
    ) -> tuple[str, dict, str]:
        history, _current_state = await self.pet_repo.load_pet_context(pet_id)

        recall_block = await self.memory_service.build_recall_block(
            pet_id, query=user_text
        )
        system_content = self.config.system_prompt + recall_block

        hist = [item for item in history if item.get("id") != current_msg_id]
        messages = self.pet_domain.base_messages(system_content, hist)

        style_block = (
            await self.style_service.render_examples_block(
                user_text, scope="reply", history=hist
            )
            if self.style_service
            else self.pet_domain.render_style_examples(user_text, scope="reply")
        )

        pre_user_system = (
            self.config.json_output_prompt
            + "\n"
            + style_block
            + "\n"
            + self.config.persona_reinforcement
        )
        messages.append({"role": "system", "content": pre_user_system})
        messages.append(
            {
                "role": "user",
                "content": self.pet_domain.wrap_user(user_text, sender_name=sender_name),
            }
        )

        content = await self.llm.chat_json(
            messages,
            max_tokens=self.config.reply_max_tokens,
            temperature=self.config.reply_temperature,
        )

        reply, speaker_name, reply_mode, structured = self.parse_llm_content(content)
        if not structured:
            if reply and reply != content.strip():
                log.warning("recovered reply from malformed JSON: %r", content[:200])
            else:
                log.warning("LLM returned non-JSON: %r", content[:200])
        if reply_mode == "unknown":
            log.warning("LLM returned missing or invalid reply_mode: %r", content[:200])

        if not reply:
            reply = self.config.fallback_replies["empty_llm"]

        log.info(
            "pet %d reply shape: mode=%s chars=%d",
            pet_id,
            reply_mode,
            len(reply),
        )

        learned_name = ""
        if (
            speaker_name
            and sender_open_id
            and not speaker_name.startswith("群友")
            and speaker_name in user_text
        ):
            if await self.system_repo.get_cached_user_name(sender_open_id) != speaker_name:
                await self.system_repo.set_cached_user_name(sender_open_id, speaker_name)
                learned_name = speaker_name
                log.info(
                    "pet %d learned name from chat: %s -> %s",
                    pet_id,
                    sender_open_id,
                    speaker_name,
                )

        now = time.time()

        def _mutator(state: dict) -> dict:
            out = dict(state)
            out["last_update_ts"] = now
            out["last_reply_ts"] = now
            out["last_proactive_answered"] = True
            return out

        new_state = await self.pet_repo.mutate_state(pet_id, _mutator)
        return reply, new_state, learned_name

    async def mark_replied(self, pet_id: int) -> None:
        await self.pet_repo.mutate_state(
            pet_id, lambda s: {**s, "last_reply_ts": time.time(), "last_proactive_answered": True}
        )

    async def is_direct_to_bot(self, chat_type: str, mentions: list[dict]) -> bool:
        if chat_type == "p2p":
            return True
        try:
            bot_open_id = await self.feishu.get_bot_open_id()
        except Exception:
            log.exception("get bot open_id failed; group message stays observer-only")
            return False
        return any((mention.get("id") or {}).get("open_id") == bot_open_id for mention in mentions)

    async def _send_response(
        self, chat_type: str, chat_id: str, message_id: str, text: str
    ) -> None:
        if chat_type == "p2p":
            await self.feishu.send_text(chat_id, text)
        else:
            await self.feishu.reply_text(message_id, text)

    async def _maybe_compress(self, pet_id: int) -> None:
        if await self.message_repo.count_unsummarized(pet_id) > self.config.compress_threshold:
            asyncio.create_task(self.memory_service.compress_pet_memory(pet_id))

    async def _caption_image_key(self, message_id: str, image_key: str) -> str | None:
        image_key = (image_key or "").strip()
        if not message_id or not image_key:
            return None
        try:
            image_bytes = await self.feishu.download_image(message_id, image_key)
        except Exception:
            log.exception("download image failed")
            return None
        if not image_bytes:
            return None
        return await self.llm.describe_image(image_bytes)

    async def _caption_image(self, message_id: str, msg: dict) -> str | None:
        try:
            content = json.loads(msg.get("content") or "{}")
        except json.JSONDecodeError:
            return None
        return await self._caption_image_key(message_id, content.get("image_key") or "")

    async def _parse_post(self, message_id: str, msg: dict) -> str | None:
        try:
            content = json.loads(msg.get("content") or "{}")
        except json.JSONDecodeError:
            return None
        paragraphs = content.get("content") or []
        if not isinstance(paragraphs, list):
            return None
        parts: list[str] = []
        title = (content.get("title") or "").strip()
        if title:
            parts.append(title)
        for para in paragraphs:
            if not isinstance(para, list):
                continue
            for el in para:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    parts.append(el.get("text") or "")
                elif tag == "at":
                    name = (el.get("user_name") or el.get("text") or "").strip()
                    if name:
                        parts.append("@" + name)
                elif tag == "img":
                    caption = await self._caption_image_key(
                        message_id, el.get("image_key") or ""
                    )
                    parts.append(
                        f"【发了一张图：{caption}】" if caption
                        else "【发了一张图，但没看清】"
                    )
        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        return text or None

    async def _mentions_with_names(self, mentions: list[dict]) -> list[dict]:
        enriched: list[dict] = []
        for mention in mentions or []:
            if not isinstance(mention, dict):
                continue
            mention = dict(mention)
            if not (mention.get("name") or "").strip():
                open_id = ((mention.get("id") or {}).get("open_id") or "").strip()
                if open_id:
                    try:
                        name = await self.resolve_user_name(open_id)
                    except Exception:
                        name = ""
                    if name:
                        mention["name"] = name
            enriched.append(mention)
        return enriched

    def _append_missing_mentions(
        self, user_text: str, mentions: list[dict], sender_open_id: str
    ) -> str:
        missing = []
        for mention in mentions or []:
            if not isinstance(mention, dict):
                continue
            name = (mention.get("name") or "").strip()
            if not name or f"@{name}" in user_text:
                continue
            open_id = ((mention.get("id") or {}).get("open_id") or "").strip()
            if open_id and open_id == sender_open_id:
                continue
            missing.append("@" + name)
        if missing:
            user_text = (user_text + " " + " ".join(missing)).strip()
        return user_text

    async def _buffer_observer_text(
        self, pet_id: int, text: str, sender_name: str, sender_open_id: str
    ) -> None:
        buf = self.runtime.observer_buffer.setdefault(pet_id, [])
        buf.append(
            {
                "content": text,
                "sender_name": sender_name,
                "open_id": sender_open_id,
                "ts": time.time(),
            }
        )
        log.info(
            "pet %d buffered observer [%s]: %r (buffer=%d)",
            pet_id,
            sender_name,
            text[:80],
            len(buf),
        )
        if len(buf) >= self.config.observer_flush_max_count:
            await self.observer_service.flush_observer_buffer(pet_id)

    async def handle_message_event(self, event: dict) -> None:
        msg = event.get("message") or {}
        message_id = msg.get("message_id")
        chat_id = msg.get("chat_id")
        if not message_id or not chat_id:
            return

        mentions = msg.get("mentions") or []
        chat_type = msg.get("chat_type") or "group"
        sender_open_id = (
            ((event.get("sender") or {}).get("sender_id") or {}).get("open_id")
        ) or ""
        sender_name = (
            await self.resolve_user_name(sender_open_id) if sender_open_id else "群友"
        )

        is_direct = await self.is_direct_to_bot(chat_type, mentions)
        msg_type = msg.get("message_type")

        if msg_type == "image":
            caption = await self._caption_image(message_id, msg)
            user_text = (
                f"【发了一张图：{caption}】" if caption
                else "【发了一张图，但没看清】"
            )
        elif msg_type == "post":
            text = await self._parse_post(message_id, msg)
            if not text:
                if is_direct:
                    log.info("ignoring empty post message (direct, sent non_text fallback)")
                    await self._send_response(chat_type, chat_id, message_id, self.config.fallback_replies["non_text"])
                return
            mentions = await self._mentions_with_names(mentions)
            user_text = self.pet_domain.clean_text(text, mentions)
            user_text = self._append_missing_mentions(user_text, mentions, sender_open_id)
            if not user_text:
                if is_direct:
                    await self._send_response(chat_type, chat_id, message_id, self.config.fallback_replies["empty_text"])
                return
        elif msg_type == "text":
            try:
                content = json.loads(msg.get("content") or "{}")
            except json.JSONDecodeError:
                if is_direct:
                    log.warning("bad content json: %r", msg.get("content"))
                return
            mentions = await self._mentions_with_names(mentions)
            user_text = self.pet_domain.clean_text(content.get("text", ""), mentions)
            user_text = self._append_missing_mentions(user_text, mentions, sender_open_id)
            if not user_text:
                if is_direct:
                    await self._send_response(chat_type, chat_id, message_id, self.config.fallback_replies["empty_text"])
                return
        elif is_direct:
            log.info("ignoring msg_type=%r (direct, sent non_text fallback)", msg_type)
            await self._send_response(chat_type, chat_id, message_id, self.config.fallback_replies["non_text"])
            return
        else:
            return

        if not is_direct:
            pet_id = await self.pet_repo.find_pet(chat_id)
            if pet_id is None:
                return
            await self._buffer_observer_text(
                pet_id, user_text, sender_name, sender_open_id
            )
            if (
                getattr(self.config, "event_interject_enabled", True)
                and getattr(self, "autonomous_service", None) is not None
            ):
                asyncio.create_task(self.maybe_event_interject(pet_id, chat_id))
            return

        pet_id = await self.pet_repo.get_or_create_pet(chat_id)
        log.info(
            "pet_id=%d chat_id=%s sender=%s user_text=%r",
            pet_id,
            chat_id,
            sender_name,
            user_text,
        )

        await self.observer_service.flush_observer_buffer(pet_id)

        # 连续消息排队与合并处理，防止无声漏答
        pending_list = self.runtime.chat_pending_messages.setdefault(pet_id, [])
        pending_list.append(
            {
                "message_id": message_id,
                "chat_type": chat_type,
                "chat_id": chat_id,
                "user_text": user_text,
                "sender_name": sender_name,
                "sender_open_id": sender_open_id,
                "ts": time.time(),
            }
        )

        async with self.runtime.chat_lock(pet_id):
            if not self.runtime.chat_pending_messages.get(pet_id):
                return
            debounce_sec = float(getattr(self.config, "reply_debounce_sec", 0.0) or 0.0)
            if debounce_sec > 0:
                await asyncio.sleep(debounce_sec)

            items = self.runtime.chat_pending_messages.pop(pet_id, [])
            if not items:
                return

            last_item = items[-1]
            last_message_id = last_item["message_id"]
            last_chat_type = last_item["chat_type"]
            last_chat_id = last_item["chat_id"]
            last_sender_open_id = last_item["sender_open_id"]
            last_sender_name = last_item["sender_name"]

            # 将合并窗口内所有真实用户消息写入数据库历史
            last_msg_id = None
            for item in items:
                last_msg_id = await self.message_repo.append_message(
                    pet_id,
                    "user",
                    item["user_text"],
                    sender_name=item["sender_name"],
                    is_observer=False,
                    sender_open_id=item["sender_open_id"],
                )

            combined_text = "\n".join(item["user_text"] for item in items)

            learned_name = ""
            try:
                reply, _reply_state, learned_name = await self.call_llm_with_memory(
                    pet_id,
                    combined_text,
                    sender_name=last_sender_name,
                    current_msg_id=last_msg_id,
                    sender_open_id=last_sender_open_id,
                )
            except Exception as exc:
                log.exception("llm error")
                reply = self.config.fallback_replies["llm_error_template"].format(
                    error_class=exc.__class__.__name__
                )
            log.info("reply=%r", reply)

            try:
                await self._send_response(last_chat_type, last_chat_id, last_message_id, reply)
            except Exception:
                log.exception("reply delivery failed for pet %d", pet_id)
                return

            await self.message_repo.append_message(pet_id, "assistant", reply)
            await self.mark_replied(pet_id)

            if learned_name:
                await self._send_response(
                    last_chat_type,
                    last_chat_id,
                    last_message_id,
                    self.config.fallback_replies["name_learned"].format(name=learned_name),
                )

            await self._maybe_compress(pet_id)

    async def maybe_event_interject(self, pet_id: int, chat_id: str) -> None:
        """群内旁听消息到来时，轻量级事件驱动的主动插话判定。"""
        debounce = float(getattr(self.config, "event_interject_debounce_sec", 2.0) or 0.0)
        if debounce > 0:
            await asyncio.sleep(debounce)

        now = time.time()
        if self.state_domain.in_quiet_hours(now):
            return

        # 若当前有直接回复正在排队或进行中，跳过主动插话
        if self.runtime.chat_pending_messages.get(pet_id):
            return
        if pet_id in self.runtime.chat_locks and self.runtime.chat_locks[pet_id].locked():
            return

        current_state = await self.pet_repo.load_pet_state(pet_id)
        last_proactive_ts = float(current_state.get("last_proactive_ts", 0) or 0)
        last_reply_ts = float(current_state.get("last_reply_ts", 0) or 0)

        cooldown = self.config.proactive_cooldown_sec
        if not current_state.get("last_proactive_answered", True):
            cooldown = getattr(self.config, "unanswered_cooldown_sec", cooldown)
        reply_cooldown = float(getattr(self.config, "proactive_reply_cooldown_sec", 60.0) or 60.0)

        if now - last_proactive_ts < cooldown or now - last_reply_ts < reply_cooldown:
            return

        # 将缓冲中的最新旁听消息落库，使评估时能读取到最新对话
        await self.observer_service.flush_observer_buffer(pet_id)

        try:
            eval_result = await self.autonomous_service.evaluate_proactive_speaking(
                pet_id, current_state
            )
            if eval_result is None:
                return

            reason, topic = eval_result
            log.info(
                "pet %d event interject triggered: reason=%r topic=%r",
                pet_id,
                reason,
                topic,
            )
            async with self.runtime.chat_lock(pet_id):
                await self.autonomous_service.proactive_speak(
                    pet_id, chat_id, reason, topic=topic
                )
        except Exception:
            log.exception("event interject failed for pet %d", pet_id)
